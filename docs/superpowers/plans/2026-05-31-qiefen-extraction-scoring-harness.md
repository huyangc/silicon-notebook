# qiefen 抽取评分 Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python harness that scores an agent-produced `pred.yaml` (same schema as the gold fixtures) against a chapter's `gold.yaml`, emitting per-stage P/R/F1, a 0–100 weighted total, and an actionable Markdown diff to drive iteration.

**Architecture:** Content-based alignment first — match predicted `evidence_atoms` to gold atoms by `source_span` IoU to build a `gold_atom_id ↔ pred_atom_id` map, then reuse that map to align every downstream stage (chunks/objects/packages/relations/mentions) even though raw IDs differ. Each stage runs a *loose* (content-only) alignment for diagnostics plus a *strict* TP rule (content + type/path) for F1. Deterministic greedy matching; an optional LLM judge (off by default) handles semantic equivalence. Pure-Python, `pyyaml`-only.

**Tech Stack:** Python 3.13, `pyyaml`, `pytest`. No scipy/numpy. All code under `fangan/testcases/harness/`.

**Working directory:** `/Users/hzf/workspace/silicon_notebook` (the main repo working tree, where `fangan/` lives — it is untracked on `master` and absent from git worktrees). Run all commands from there.

**Git note:** The whole `fangan/` tree is currently untracked on `master`. Commit steps below are written against a feature branch `harness/qiefen-scorer`. If the user prefers not to commit yet, skip the `git commit` steps — every other step stands alone. Create the branch once before Task 1 if committing: `git checkout -b harness/qiefen-scorer`.

**Spec:** `docs/superpowers/specs/2026-05-31-qiefen-extraction-scoring-harness-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `fangan/testcases/harness/__init__.py` | package marker |
| `fangan/testcases/harness/config.py` | stage weights, thresholds, object-match sub-weights |
| `fangan/testcases/harness/textnorm.py` | `norm_text`, `values_of_payload`, equivalence helpers |
| `fangan/testcases/harness/metrics.py` | `prf`, `jaccard`, aggregation of stage scores → weighted total |
| `fangan/testcases/harness/align.py` | `span_iou`, generic greedy matcher, per-stage aligners |
| `fangan/testcases/harness/stages.py` | per-stage scorers returning `{score, prf, details}` |
| `fangan/testcases/harness/judge.py` | optional LLM equivalence interface + content-hash cache (default OFF) |
| `fangan/testcases/harness/report.py` | JSON + Markdown emitters |
| `fangan/testcases/harness/score.py` | CLI: score ONE chapter |
| `fangan/testcases/harness/run_all.py` | score a candidate tree vs all chapters → aggregate + leaderboard |
| `fangan/testcases/harness/README.md` | usage |
| `fangan/testcases/harness/tests/` | pytest suite (one file per module + integration) |

Data model: we operate directly on `yaml.safe_load` dicts. A loaded fixture is a `dict` with top-level keys `source_meta, source_elements, section_tree, evidence_atoms, semantic_chunks, context_packages, objects, relations, mentions, do_not_extract` (any may be missing → treat as `[]`).

Shared alignment return type (plain dict, used everywhere):
```python
# Alignment = {
#   "matches": [(gold_id, pred_id, score), ...],   # loose matches, score desc
#   "g2p": {gold_id: pred_id}, "p2g": {pred_id: gold_id},
#   "unmatched_gold": [gold_id, ...], "unmatched_pred": [pred_id, ...],
# }
```

---

## Task 1: Package scaffold + config

**Files:**
- Create: `fangan/testcases/harness/__init__.py`
- Create: `fangan/testcases/harness/config.py`
- Test: `fangan/testcases/harness/tests/__init__.py`, `fangan/testcases/harness/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_config.py`:
```python
from harness import config

def test_weights_sum_to_one():
    assert abs(sum(config.WEIGHTS.values()) - 1.0) < 1e-9

def test_thresholds_present():
    for k in ("atom_iou", "chunk_jaccard", "object_match"):
        assert 0.0 <= config.THRESHOLDS[k] <= 1.0

def test_object_match_weights_sum_to_one():
    assert abs(sum(config.OBJECT_MATCH_WEIGHTS.values()) - 1.0) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness'`.

- [ ] **Step 3: Create package files**

`fangan/testcases/harness/__init__.py`:
```python
"""qiefen extraction scoring harness."""
```
`fangan/testcases/harness/tests/__init__.py`: (empty file)

`fangan/testcases/harness/config.py`:
```python
"""Tunable weights and thresholds — the single place to adjust scoring."""

# Stage buckets; must sum to 1.0. Reflects qiefen emphasis (evidence + extraction).
WEIGHTS = {
    "evidence_atoms": 0.20,
    "semantic_chunks": 0.15,
    "objects": 0.12,          # object existence (type-strict F1)
    "object_payload": 0.13,   # payload-field F1 over loosely-matched objects
    "object_evidence": 0.10,  # local-evidence Jaccard over loosely-matched objects
    "relations": 0.15,
    "context_packages": 0.05,
    "do_not_extract": 0.05,
    "structure": 0.05,        # section_tree + mentions combined
}

THRESHOLDS = {
    "atom_iou": 0.5,        # min source_span IoU to align two atoms
    "chunk_jaccard": 0.5,   # min mapped-atom-set Jaccard to align two chunks
    "object_match": 0.4,    # min composite score to align two objects
    "mention_text": 0.6,    # min text similarity to align two mentions
}

# Composite object-match score = weighted sum of these (must sum to 1.0).
OBJECT_MATCH_WEIGHTS = {"type": 0.4, "evidence": 0.4, "payload": 0.2}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_config.py -q`
Expected: PASS (3 passed). Note: run pytest from `fangan/testcases` so `import harness` resolves.

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/__init__.py fangan/testcases/harness/config.py fangan/testcases/harness/tests/__init__.py fangan/testcases/harness/tests/test_config.py
git commit -m "feat(harness): package scaffold + scoring config"
```

---

## Task 2: Text normalization helpers

**Files:**
- Create: `fangan/testcases/harness/textnorm.py`
- Test: `fangan/testcases/harness/tests/test_textnorm.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_textnorm.py`:
```python
from harness import textnorm

def test_norm_collapses_ws_and_lowercases():
    assert textnorm.norm_text("  The   U-Shaped  Law ") == "the u-shaped law"

def test_norm_strips_quotes():
    assert textnorm.norm_text("'Engram'") == "engram"

def test_text_equiv_exact_after_norm():
    assert textnorm.text_equiv("O(1)  lookup", "o(1) lookup") is True

def test_text_equiv_containment():
    # short gold value contained in longer pred value counts as equivalent
    assert textnorm.text_equiv("MMLU +3.4", "knowledge: MMLU +3.4 and CMMLU +4.0") is True

def test_text_equiv_negative():
    assert textnorm.text_equiv("deterministic addressing", "random eviction") is False

def test_payload_values_flatten_nested():
    payload = {"a": "x", "b": {"c": "y", "d": ["m", "n"]}}
    vals = textnorm.payload_values(payload)
    assert set(vals) == {"x", "y", "m", "n"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_textnorm.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.textnorm'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/textnorm.py`:
```python
"""Deterministic text normalization + equivalence (judge-free fallback)."""
import re

_WS = re.compile(r"\s+")


def norm_text(s):
    if s is None:
        return ""
    s = str(s).strip()
    # strip a single layer of surrounding quotes
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1]
    s = _WS.sub(" ", s)
    return s.strip().lower()


def text_equiv(gold, pred, judge=None):
    """Deterministic equivalence: equal after norm, or short side contained in long side.

    `judge` (optional callable(gold, pred)->bool) is consulted only when the
    deterministic check fails; default None => purely deterministic.
    """
    g, p = norm_text(gold), norm_text(pred)
    if not g and not p:
        return True
    if not g or not p:
        return False
    if g == p:
        return True
    shorter, longer = (g, p) if len(g) <= len(p) else (p, g)
    if len(shorter) >= 4 and shorter in longer:
        return True
    if judge is not None:
        return bool(judge(gold, pred))
    return False


def payload_values(payload):
    """Flatten a payload dict into a list of scalar string values (keys dropped)."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif v is not None:
            out.append(str(v))

    walk(payload or {})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_textnorm.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/textnorm.py fangan/testcases/harness/tests/test_textnorm.py
git commit -m "feat(harness): text normalization + equivalence helpers"
```

---

## Task 3: Metrics primitives (prf, jaccard)

**Files:**
- Create: `fangan/testcases/harness/metrics.py`
- Test: `fangan/testcases/harness/tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_metrics.py`:
```python
from harness import metrics

def test_prf_perfect():
    r = metrics.prf(tp=5, fp=0, fn=0)
    assert r["precision"] == 1.0 and r["recall"] == 1.0 and r["f1"] == 1.0

def test_prf_zero_when_empty():
    r = metrics.prf(tp=0, fp=0, fn=0)
    # empty-vs-empty is defined as perfect (nothing to find, nothing wrong)
    assert r["f1"] == 1.0

def test_prf_half():
    r = metrics.prf(tp=1, fp=1, fn=1)
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5

def test_jaccard():
    assert metrics.jaccard({1, 2, 3}, {2, 3, 4}) == 0.5
    assert metrics.jaccard(set(), set()) == 1.0
    assert metrics.jaccard({1}, set()) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.metrics'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/metrics.py`:
```python
"""P/R/F1, Jaccard, and weighted aggregation."""
from . import config


def prf(tp, fp, fn):
    if tp == 0 and fp == 0 and fn == 0:
        return {"tp": 0, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def weighted_total(stage_scores):
    """stage_scores: {bucket_name: score_in_0_1}. Returns 0..100.

    Buckets absent from stage_scores are treated as perfect (1.0) so the total
    stays comparable; callers always supply all WEIGHTS keys in practice.
    """
    total = 0.0
    for bucket, w in config.WEIGHTS.items():
        total += w * float(stage_scores.get(bucket, 1.0))
    return round(100.0 * total, 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_metrics.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/metrics.py fangan/testcases/harness/tests/test_metrics.py
git commit -m "feat(harness): prf/jaccard/weighted-total metrics"
```

---

## Task 4: Span IoU + generic greedy matcher

**Files:**
- Create: `fangan/testcases/harness/align.py`
- Test: `fangan/testcases/harness/tests/test_align_core.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_align_core.py`:
```python
from harness import align

def span(line, c0, c1, f="x.md"):
    return {"file": f, "line_start": line, "line_end": line, "char_start": c0, "char_end": c1}

def test_iou_identical():
    assert align.span_iou(span(1, 0, 100), span(1, 0, 100)) == 1.0

def test_iou_disjoint():
    assert align.span_iou(span(1, 0, 50), span(1, 60, 100)) == 0.0

def test_iou_half_overlap():
    # [0,100) vs [50,150): overlap 50, union 150
    assert abs(align.span_iou(span(1, 0, 100), span(1, 50, 150)) - (50 / 150)) < 1e-6

def test_iou_different_file_is_zero():
    assert align.span_iou(span(1, 0, 100, "a.md"), span(1, 0, 100, "b.md")) == 0.0

def test_iou_different_line_low():
    # different lines => intervals far apart in encoded space => 0 overlap
    assert align.span_iou(span(1, 0, 100), span(9, 0, 100)) == 0.0

def test_greedy_match_picks_best():
    # gold g1,g2 ; pred p1,p2 ; scores favour g1-p1 and g2-p2
    scores = {("g1", "p1"): 0.9, ("g1", "p2"): 0.2, ("g2", "p1"): 0.1, ("g2", "p2"): 0.8}
    al = align.greedy(["g1", "g2"], ["p1", "p2"], scores, thresh=0.5)
    assert al["g2p"] == {"g1": "p1", "g2": "p2"}
    assert al["unmatched_gold"] == [] and al["unmatched_pred"] == []

def test_greedy_threshold_drops_low():
    scores = {("g1", "p1"): 0.3}
    al = align.greedy(["g1"], ["p1"], scores, thresh=0.5)
    assert al["g2p"] == {}
    assert al["unmatched_gold"] == ["g1"] and al["unmatched_pred"] == ["p1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_align_core.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.align'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/align.py`:
```python
"""Content-based alignment: span IoU + a deterministic greedy matcher."""

_BIG = 1_000_000.0  # > any line length in these fixtures; encodes (line,char) as a float


def _enc(line, char):
    return float(line) + (float(char) / _BIG)


def span_iou(s1, s2):
    """IoU of two source_span dicts. 0 if files differ or no overlap.

    Position encoded as line + char/1e6 so single-line spans give exact char IoU
    and multi-line spans stay monotonic (assumes line length < 1e6).
    """
    if not s1 or not s2:
        return 0.0
    if s1.get("file") != s2.get("file"):
        return 0.0
    a0 = _enc(s1["line_start"], s1["char_start"])
    a1 = _enc(s1["line_end"], s1["char_end"])
    b0 = _enc(s2["line_start"], s2["char_start"])
    b1 = _enc(s2["line_end"], s2["char_end"])
    lo, hi = max(a0, b0), min(a1, b1)
    overlap = max(0.0, hi - lo)
    union = (a1 - a0) + (b1 - b0) - overlap
    if union <= 0:
        return 1.0 if overlap > 0 or (a1 == a0 and b1 == b0 and a0 == b0) else 0.0
    return overlap / union


def greedy(gold_ids, pred_ids, scores, thresh):
    """Greedy max-score one-to-one matching.

    scores: {(gold_id, pred_id): score}. Pairs with score < thresh are ignored.
    Ties broken by (gold_id, pred_id) lexicographic order for determinism.
    Returns the Alignment dict described in the plan header.
    """
    ranked = sorted(
        ((s, g, p) for (g, p), s in scores.items() if s >= thresh),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    g2p, p2g = {}, {}
    matches = []
    for s, g, p in ranked:
        if g in g2p or p in p2g:
            continue
        g2p[g] = p
        p2g[p] = g
        matches.append((g, p, s))
    unmatched_gold = [g for g in gold_ids if g not in g2p]
    unmatched_pred = [p for p in pred_ids if p not in p2g]
    return {
        "matches": matches,
        "g2p": g2p,
        "p2g": p2g,
        "unmatched_gold": unmatched_gold,
        "unmatched_pred": unmatched_pred,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_align_core.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/align.py fangan/testcases/harness/tests/test_align_core.py
git commit -m "feat(harness): span IoU + greedy matcher"
```

---

## Task 5: Atom alignment + atom stage scorer

**Files:**
- Modify: `fangan/testcases/harness/align.py` (add `match_atoms`)
- Create: `fangan/testcases/harness/stages.py` (add `score_atoms`)
- Test: `fangan/testcases/harness/tests/test_atoms.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_atoms.py`:
```python
from harness import align, stages

def atom(aid, c0, c1, atype="claim_sentence", line=11):
    return {"id": aid, "atom_type": atype,
            "source_span": {"file": "s.md", "line_start": line, "line_end": line,
                            "char_start": c0, "char_end": c1}}

GOLD = [atom("A1", 0, 100), atom("A2", 100, 200, "result_sentence")]

def test_match_atoms_identical():
    al = align.match_atoms(GOLD, GOLD, thresh=0.5)
    assert al["g2p"] == {"A1": "A1", "A2": "A2"}

def test_match_atoms_by_span_not_id():
    pred = [atom("P9", 2, 99), atom("P8", 101, 199, "result_sentence")]
    al = align.match_atoms(GOLD, pred, thresh=0.5)
    assert al["g2p"] == {"A1": "P9", "A2": "P8"}

def test_score_atoms_perfect():
    res = stages.score_atoms(GOLD, GOLD)
    assert res["prf"]["f1"] == 1.0
    assert res["type_accuracy"] == 1.0
    assert res["score"] == 1.0

def test_score_atoms_type_mismatch_hits_f1():
    pred = [atom("P1", 0, 100, "WRONG"), atom("P2", 100, 200, "result_sentence")]
    res = stages.score_atoms(GOLD, pred)
    # A1 loosely matches P1 but wrong type => A1 is fn, P1 is fp; A2/P2 are tp
    assert res["prf"]["tp"] == 1
    assert res["type_accuracy"] == 0.5
    assert res["score"] < 1.0
    assert res["type_mismatches"] == [{"gold_id": "A1", "pred_id": "P1",
                                       "gold_type": "claim_sentence", "pred_type": "WRONG"}]

def test_score_atoms_missing_atom_lowers_recall():
    res = stages.score_atoms(GOLD, [GOLD[0]])
    assert res["prf"]["fn"] == 1
    assert res["prf"]["recall"] == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_atoms.py -q`
Expected: FAIL — `AttributeError: module 'harness.align' has no attribute 'match_atoms'`.

- [ ] **Step 3: Implement `match_atoms` and `score_atoms`**

Append to `fangan/testcases/harness/align.py`:
```python
def match_atoms(gold_atoms, pred_atoms, thresh):
    """Align atoms by source_span IoU."""
    gids = [a["id"] for a in gold_atoms]
    pids = [a["id"] for a in pred_atoms]
    gspan = {a["id"]: a.get("source_span") for a in gold_atoms}
    pspan = {a["id"]: a.get("source_span") for a in pred_atoms}
    scores = {}
    for g in gids:
        for p in pids:
            iou = span_iou(gspan[g], pspan[p])
            if iou > 0:
                scores[(g, p)] = iou
    return greedy(gids, pids, scores, thresh)
```

Create `fangan/testcases/harness/stages.py`:
```python
"""Per-stage scorers. Each returns {'score': float0_1, 'prf': {...}, ...details}."""
from . import align, metrics, textnorm
from .config import THRESHOLDS


def _by_id(items):
    return {it["id"]: it for it in (items or [])}


def score_atoms(gold_atoms, pred_atoms):
    gold_atoms = gold_atoms or []
    pred_atoms = pred_atoms or []
    al = align.match_atoms(gold_atoms, pred_atoms, THRESHOLDS["atom_iou"])
    g = _by_id(gold_atoms)
    p = _by_id(pred_atoms)

    type_mismatches = []
    type_ok = 0
    ious = []
    for gid, pid, iou in al["matches"]:
        ious.append(iou)
        if g[gid].get("atom_type") == p[pid].get("atom_type"):
            type_ok += 1
        else:
            type_mismatches.append({
                "gold_id": gid, "pred_id": pid,
                "gold_type": g[gid].get("atom_type"), "pred_type": p[pid].get("atom_type"),
            })

    n_matched = len(al["matches"])
    tp = type_ok  # strict TP requires correct atom_type
    wrong_type = n_matched - type_ok
    fp = len(al["unmatched_pred"]) + wrong_type
    fn = len(al["unmatched_gold"]) + wrong_type
    pr = metrics.prf(tp, fp, fn)
    type_accuracy = (type_ok / n_matched) if n_matched else 1.0
    mean_iou = (sum(ious) / len(ious)) if ious else 1.0
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": type_accuracy,
        "mean_iou": round(mean_iou, 4),
        "type_mismatches": type_mismatches,
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_atoms.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/align.py fangan/testcases/harness/stages.py fangan/testcases/harness/tests/test_atoms.py
git commit -m "feat(harness): atom alignment + atom stage scorer"
```

---

## Task 6: Chunk stage scorer (via atom map)

**Files:**
- Modify: `fangan/testcases/harness/stages.py` (add `score_chunks`)
- Test: `fangan/testcases/harness/tests/test_chunks.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_chunks.py`:
```python
from harness import stages

# atom alignment map: pred atom id -> gold atom id
P2G = {"pa": "A1", "pb": "A2", "pc": "A3"}

def chunk(cid, atoms, ctype="article_core_claim_block"):
    return {"id": cid, "chunk_type": ctype, "atom_ids": atoms}

GOLD = [chunk("C1", ["A1", "A2"]), chunk("C2", ["A3"])]

def test_chunks_perfect():
    pred = [chunk("X", ["pa", "pb"]), chunk("Y", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["prf"]["f1"] == 1.0
    assert res["score"] == 1.0
    assert res["type_accuracy"] == 1.0

def test_chunks_oversplit_detected():
    # pred splits gold C1's atoms into two chunks => C1 matches at most one, other is spurious
    pred = [chunk("X", ["pa"]), chunk("Y", ["pb"]), chunk("Z", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["over_split"] >= 1
    assert res["prf"]["fp"] >= 1

def test_chunks_type_mismatch():
    pred = [chunk("X", ["pa", "pb"], "WRONG"), chunk("Y", ["pc"])]
    res = stages.score_chunks(GOLD, pred, P2G)
    assert res["type_accuracy"] == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_chunks.py -q`
Expected: FAIL — `AttributeError: module 'harness.stages' has no attribute 'score_chunks'`.

- [ ] **Step 3: Implement `score_chunks`**

Append to `fangan/testcases/harness/stages.py`:
```python
def _map_atoms(atom_ids, p2g):
    """Translate predicted atom ids into gold-atom space; drop unmappable."""
    return {p2g[a] for a in (atom_ids or []) if a in p2g}


def score_chunks(gold_chunks, pred_chunks, atom_p2g):
    gold_chunks = gold_chunks or []
    pred_chunks = pred_chunks or []
    gid = [c["id"] for c in gold_chunks]
    pid = [c["id"] for c in pred_chunks]
    gset = {c["id"]: set(c.get("atom_ids") or []) for c in gold_chunks}
    pset = {c["id"]: _map_atoms(c.get("atom_ids"), atom_p2g) for c in pred_chunks}

    scores = {}
    for g in gid:
        for p in pid:
            j = metrics.jaccard(gset[g], pset[p])
            if j > 0:
                scores[(g, p)] = j
    al = align.greedy(gid, pid, scores, THRESHOLDS["chunk_jaccard"])

    gtype = {c["id"]: c.get("chunk_type") for c in gold_chunks}
    ptype = {c["id"]: c.get("chunk_type") for c in pred_chunks}
    type_ok = 0
    type_mismatches = []
    for g, p, _ in al["matches"]:
        if gtype[g] == ptype[p]:
            type_ok += 1
        else:
            type_mismatches.append({"gold_id": g, "pred_id": p,
                                    "gold_type": gtype[g], "pred_type": ptype[p]})
    n = len(al["matches"])
    wrong = n - type_ok
    pr = metrics.prf(type_ok, len(al["unmatched_pred"]) + wrong, len(al["unmatched_gold"]) + wrong)
    # over/under split heuristics: matched-pred whose atom set is a strict subset/superset
    over_split = 0
    under_split = 0
    for g, p, _ in al["matches"]:
        if pset[p] and pset[p] < gset[g]:
            over_split += 1
        if gset[g] and gset[g] < pset[p]:
            under_split += 1
    over_split += len(al["unmatched_pred"])  # extra chunks are over-splitting symptoms
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": (type_ok / n) if n else 1.0,
        "over_split": over_split,
        "under_split": under_split,
        "type_mismatches": type_mismatches,
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_chunks.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/stages.py fangan/testcases/harness/tests/test_chunks.py
git commit -m "feat(harness): semantic-chunk stage scorer"
```

---

## Task 7: Object alignment + object/payload/evidence scorers

**Files:**
- Modify: `fangan/testcases/harness/align.py` (add `match_objects`)
- Modify: `fangan/testcases/harness/stages.py` (add `score_objects`)
- Test: `fangan/testcases/harness/tests/test_objects.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_objects.py`:
```python
from harness import align, stages

P2G = {"pa": "A1", "pb": "A2", "pc": "A3"}

def obj(oid, otype, local, payload, home="PKG1"):
    return {"id": oid, "type": otype, "home_package": home,
            "local_evidence_atom_ids": local, "payload": payload,
            "supporting_context_atom_ids": []}

GOLD = [
    obj("O1", "ArticleClaim", ["A1", "A2"], {"statement": "conditional memory is a new sparsity axis"}),
    obj("O2", "ArticleMethod", ["A3"], {"name": "Engram", "mechanism": "O(1) lookup"}),
]

def test_objects_perfect_self():
    res = stages.score_objects(GOLD, GOLD, {a: a for a in ["A1", "A2", "A3"]})
    assert res["prf"]["f1"] == 1.0
    assert res["payload"]["f1"] == 1.0
    assert res["evidence"]["mean_jaccard"] == 1.0
    assert res["score"] == 1.0

def test_objects_match_by_content_not_id():
    pred = [
        obj("Z1", "ArticleClaim", ["pa", "pb"], {"claim": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram", "how": "O(1) lookup"}),
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["alignment"]["g2p"] == {"O1": "Z1", "O2": "Z2"}
    # payload values captured despite different keys
    assert res["payload"]["f1"] == 1.0

def test_objects_type_mismatch_hits_f1():
    pred = [
        obj("Z1", "WRONGTYPE", ["pa", "pb"], {"statement": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram", "mechanism": "O(1) lookup"}),
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["prf"]["tp"] == 1
    assert any(tm["gold_id"] == "O1" for tm in res["type_mismatches"])

def test_objects_missing_payload_field_lowers_payload_recall():
    pred = [
        obj("Z1", "ArticleClaim", ["pa", "pb"], {"statement": "conditional memory is a new sparsity axis"}),
        obj("Z2", "ArticleMethod", ["pc"], {"name": "Engram"}),  # dropped mechanism
    ]
    res = stages.score_objects(GOLD, pred, P2G)
    assert res["payload"]["recall"] < 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_objects.py -q`
Expected: FAIL — `AttributeError: module 'harness.align' has no attribute 'match_objects'`.

- [ ] **Step 3: Implement `match_objects` and `score_objects`**

Append to `fangan/testcases/harness/align.py`:
```python
from . import metrics as _metrics
from . import textnorm as _textnorm
from .config import OBJECT_MATCH_WEIGHTS


def _payload_value_overlap(gp, pp):
    gvals = [_textnorm.norm_text(v) for v in _textnorm.payload_values(gp)]
    pvals = [_textnorm.norm_text(v) for v in _textnorm.payload_values(pp)]
    if not gvals and not pvals:
        return 1.0
    if not gvals or not pvals:
        return 0.0
    matched = 0
    pool = list(pvals)
    for gv in gvals:
        for i, pv in enumerate(pool):
            if gv and pv and (gv == pv or (len(gv) >= 4 and gv in pv) or (len(pv) >= 4 and pv in gv)):
                matched += 1
                pool.pop(i)
                break
    return matched / max(len(gvals), len(pvals))


def object_pair_score(gold_obj, pred_obj, atom_p2g):
    type_s = 1.0 if gold_obj.get("type") == pred_obj.get("type") else 0.0
    gloc = set(gold_obj.get("local_evidence_atom_ids") or [])
    ploc = {atom_p2g[a] for a in (pred_obj.get("local_evidence_atom_ids") or []) if a in atom_p2g}
    ev_s = _metrics.jaccard(gloc, ploc)
    pay_s = _payload_value_overlap(gold_obj.get("payload"), pred_obj.get("payload"))
    w = OBJECT_MATCH_WEIGHTS
    return w["type"] * type_s + w["evidence"] * ev_s + w["payload"] * pay_s


def match_objects(gold_objs, pred_objs, atom_p2g, thresh):
    gids = [o["id"] for o in gold_objs]
    pids = [o["id"] for o in pred_objs]
    gobj = {o["id"]: o for o in gold_objs}
    pobj = {o["id"]: o for o in pred_objs}
    scores = {}
    for g in gids:
        for p in pids:
            s = object_pair_score(gobj[g], pobj[p], atom_p2g)
            if s > 0:
                scores[(g, p)] = s
    return greedy(gids, pids, scores, thresh)
```

Append to `fangan/testcases/harness/stages.py`:
```python
def _payload_field_prf(gold_objs, pred_objs, matches, judge=None):
    """Aggregate payload value-level P/R/F1 over loosely-matched object pairs."""
    g = _by_id(gold_objs)
    p = _by_id(pred_objs)
    tp = fp = fn = 0
    gaps = []
    for gid, pid, _ in matches:
        gvals = textnorm.payload_values(g[gid].get("payload"))
        pvals = textnorm.payload_values(p[pid].get("payload"))
        pool = list(pvals)
        local_tp = 0
        missed_vals = []
        for gv in gvals:
            hit = None
            for i, pv in enumerate(pool):
                if textnorm.text_equiv(gv, pv, judge=judge):
                    hit = i
                    break
            if hit is not None:
                pool.pop(hit)
                local_tp += 1
            else:
                missed_vals.append(gv)
        tp += local_tp
        fn += len(gvals) - local_tp
        fp += len(pool)
        if missed_vals:
            gaps.append({"gold_id": gid, "pred_id": pid, "missing_values": missed_vals})
    pr = metrics.prf(tp, fp, fn)
    pr["gaps"] = gaps
    return pr


def score_objects(gold_objs, pred_objs, atom_p2g, judge=None):
    gold_objs = gold_objs or []
    pred_objs = pred_objs or []
    al = align.match_objects(gold_objs, pred_objs, atom_p2g, THRESHOLDS["object_match"])
    g = _by_id(gold_objs)
    p = _by_id(pred_objs)

    type_ok = 0
    type_mismatches = []
    ev_jaccards = []
    for gid, pid, _ in al["matches"]:
        if g[gid].get("type") == p[pid].get("type"):
            type_ok += 1
        else:
            type_mismatches.append({"gold_id": gid, "pred_id": pid,
                                    "gold_type": g[gid].get("type"), "pred_type": p[pid].get("type")})
        gloc = set(g[gid].get("local_evidence_atom_ids") or [])
        ploc = {atom_p2g[a] for a in (p[pid].get("local_evidence_atom_ids") or []) if a in atom_p2g}
        ev_jaccards.append(metrics.jaccard(gloc, ploc))

    n = len(al["matches"])
    wrong = n - type_ok
    pr = metrics.prf(type_ok, len(al["unmatched_pred"]) + wrong, len(al["unmatched_gold"]) + wrong)
    payload = _payload_field_prf(gold_objs, pred_objs, al["matches"], judge=judge)
    mean_jac = (sum(ev_jaccards) / len(ev_jaccards)) if ev_jaccards else 1.0
    return {
        "score": pr["f1"],
        "prf": pr,
        "type_accuracy": (type_ok / n) if n else 1.0,
        "type_mismatches": type_mismatches,
        "payload": {"precision": payload["precision"], "recall": payload["recall"],
                    "f1": payload["f1"], "gaps": payload["gaps"]},
        "evidence": {"mean_jaccard": round(mean_jac, 4)},
        "missed": al["unmatched_gold"],
        "spurious": al["unmatched_pred"],
        "alignment": al,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_objects.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/align.py fangan/testcases/harness/stages.py fangan/testcases/harness/tests/test_objects.py
git commit -m "feat(harness): object alignment + object/payload/evidence scorers"
```

---

## Task 8: Relations + context_packages + structure + do_not_extract scorers

**Files:**
- Modify: `fangan/testcases/harness/stages.py` (add `score_relations`, `score_packages`, `score_structure`, `score_do_not_extract`)
- Test: `fangan/testcases/harness/tests/test_relations_etc.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_relations_etc.py`:
```python
from harness import stages

# object alignment: gold_obj_id -> pred_obj_id and inverse
OBJ_G2P = {"O1": "Z1", "O2": "Z2"}
OBJ_P2G = {"Z1": "O1", "Z2": "O2"}

def rel(rid, rtype, src, tgt):
    return {"id": rid, "relation_type": rtype, "source_object_id": src, "target_object_id": tgt}

def test_relations_perfect():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = [rel("PR", "method_addresses_problem", "Z1", "Z2")]
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["f1"] == 1.0
    assert res["score"] == 1.0

def test_relations_type_mismatch():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = [rel("PR", "result_supports_claim", "Z1", "Z2")]
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["tp"] == 0
    assert res["type_mismatches"]

def test_relations_endpoint_not_aligned_is_fn():
    gold = [rel("R1", "method_addresses_problem", "O1", "O2")]
    pred = []  # nothing
    res = stages.score_relations(gold, pred, OBJ_G2P, OBJ_P2G)
    assert res["prf"]["fn"] == 1

def test_packages_object_recall():
    gold_pkgs = [{"id": "PKG1", "chunk_id": "C1", "expected_objects": ["O1", "O2"],
                  "expected_local_fields": {}}]
    pred_pkgs = [{"id": "QP", "chunk_id": "X"}]
    # both objects homed into QP by pred; chunk alignment maps C1->X
    pred_objs = [{"id": "Z1", "home_package": "QP"}, {"id": "Z2", "home_package": "QP"}]
    res = stages.score_packages(gold_pkgs, pred_pkgs, pred_objs,
                                chunk_g2p={"C1": "X"}, obj_g2p=OBJ_G2P)
    assert res["object_recall"] == 1.0
    assert res["score"] == 1.0

def test_structure_section_paths():
    gold_tree = [{"id": "S1", "path": "Abstract"}, {"id": "S2", "path": "1 Introduction"}]
    pred_tree = [{"id": "x", "path": "abstract"}]  # case-insensitive match, missing one
    res = stages.score_structure(gold_tree, pred_tree, gold_mentions=[], pred_mentions=[],
                                 atom_p2g={})
    assert res["sections"]["recall"] == 0.5

def test_do_not_extract_violation():
    gold_dne = [{"text": "https://github.com/deepseek-ai/Engram", "kind": "out_of_slice_reference"}]
    # pred extracted a mention containing the forbidden url
    pred = {"mentions": [{"id": "m", "text": "https://github.com/deepseek-ai/Engram", "type": "Concept"}],
            "objects": [], "evidence_atoms": []}
    res = stages.score_do_not_extract(gold_dne, pred)
    assert res["violations"] == 1
    assert res["score"] < 1.0

def test_do_not_extract_clean():
    gold_dne = [{"text": "https://github.com/deepseek-ai/Engram"}]
    pred = {"mentions": [], "objects": [], "evidence_atoms": []}
    res = stages.score_do_not_extract(gold_dne, pred)
    assert res["violations"] == 0
    assert res["score"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_relations_etc.py -q`
Expected: FAIL — `AttributeError: module 'harness.stages' has no attribute 'score_relations'`.

- [ ] **Step 3: Implement the four scorers**

Append to `fangan/testcases/harness/stages.py`:
```python
def score_relations(gold_rels, pred_rels, obj_g2p, obj_p2g):
    gold_rels = gold_rels or []
    pred_rels = pred_rels or []
    # Index pred relations by their endpoints translated to gold-object space.
    pred_by_endpoints = {}
    for r in pred_rels:
        s = obj_p2g.get(r.get("source_object_id"))
        t = obj_p2g.get(r.get("target_object_id"))
        pred_by_endpoints.setdefault((s, t), []).append(r)

    tp = 0
    type_mismatches = []
    missed = []
    used_pred = set()
    for r in gold_rels:
        key = (r.get("source_object_id"), r.get("target_object_id"))
        cands = [pr for pr in pred_by_endpoints.get(key, []) if id(pr) not in used_pred]
        if not cands:
            missed.append(r["id"])
            continue
        # prefer a candidate with matching relation_type
        match = next((pr for pr in cands if pr.get("relation_type") == r.get("relation_type")), None)
        if match is not None:
            tp += 1
            used_pred.add(id(match))
        else:
            chosen = cands[0]
            used_pred.add(id(chosen))
            type_mismatches.append({"gold_id": r["id"], "pred_id": chosen.get("id"),
                                    "gold_type": r.get("relation_type"),
                                    "pred_type": chosen.get("relation_type")})
    wrong = len(type_mismatches)
    spurious = [pr.get("id") for pr in pred_rels if id(pr) not in used_pred]
    fp = len(spurious) + wrong
    fn = len(missed) + wrong
    pr = metrics.prf(tp, fp, fn)
    return {"score": pr["f1"], "prf": pr, "type_mismatches": type_mismatches,
            "missed": missed, "spurious": spurious}


def score_packages(gold_pkgs, pred_pkgs, pred_objs, chunk_g2p, obj_g2p):
    gold_pkgs = gold_pkgs or []
    if not gold_pkgs:
        return {"score": 1.0, "object_recall": 1.0, "local_field_coverage": 1.0, "details": []}
    pred_home = {o["id"]: o.get("home_package") for o in (pred_objs or [])}
    total_obj = matched_obj = 0
    total_fields = matched_fields = 0
    details = []
    for pkg in gold_pkgs:
        want_pkg_pred = chunk_g2p.get(pkg.get("chunk_id"))
        rec_hits = []
        for gobj in (pkg.get("expected_objects") or []):
            total_obj += 1
            pobj = obj_g2p.get(gobj)
            ok = pobj is not None and want_pkg_pred is not None and pred_home.get(pobj) == want_pkg_pred
            if ok:
                matched_obj += 1
            else:
                rec_hits.append(gobj)
        for _gobj, fields in (pkg.get("expected_local_fields") or {}).items():
            total_fields += len(fields or [])
            # coverage credited only if the object is correctly homed here
            pobj = obj_g2p.get(_gobj)
            if pobj is not None and pred_home.get(pobj) == want_pkg_pred:
                matched_fields += len(fields or [])
        if rec_hits:
            details.append({"package": pkg["id"], "missed_expected_objects": rec_hits})
    object_recall = (matched_obj / total_obj) if total_obj else 1.0
    field_cov = (matched_fields / total_fields) if total_fields else 1.0
    score = 0.7 * object_recall + 0.3 * field_cov
    return {"score": score, "object_recall": object_recall,
            "local_field_coverage": field_cov, "details": details}


def score_structure(gold_tree, pred_tree, gold_mentions, pred_mentions, atom_p2g):
    # sections matched by normalized path
    gpaths = {textnorm.norm_text(n.get("path")) for n in (gold_tree or [])}
    ppaths = {textnorm.norm_text(n.get("path")) for n in (pred_tree or [])}
    s_tp = len(gpaths & ppaths)
    s_pr = metrics.prf(s_tp, len(ppaths - gpaths), len(gpaths - ppaths))
    # mentions matched by (mapped atom id, normalized text)
    def mkey(m, mapper):
        aid = m.get("atom_id")
        aid = mapper.get(aid, aid) if mapper else aid
        return (aid, textnorm.norm_text(m.get("text")))
    gset = {mkey(m, None) for m in (gold_mentions or [])}
    pset = {mkey(m, atom_p2g) for m in (pred_mentions or [])}
    m_tp = len(gset & pset)
    m_pr = metrics.prf(m_tp, len(pset - gset), len(gset - pset))
    score = 0.5 * s_pr["f1"] + 0.5 * m_pr["f1"]
    return {"score": score, "sections": s_pr, "mentions": m_pr}


def score_do_not_extract(gold_dne, pred):
    gold_dne = gold_dne or []
    # Collect all extracted text surfaces from pred.
    surfaces = []
    for a in (pred.get("evidence_atoms") or []):
        surfaces.append(textnorm.norm_text(a.get("raw_text")))
        surfaces.append(textnorm.norm_text(a.get("normalized_text")))
    for m in (pred.get("mentions") or []):
        surfaces.append(textnorm.norm_text(m.get("text")))
    for o in (pred.get("objects") or []):
        for v in textnorm.payload_values(o.get("payload")):
            surfaces.append(textnorm.norm_text(v))
    surfaces = [s for s in surfaces if s]

    def forbidden_texts(entry):
        if entry.get("text"):
            return [entry["text"]]
        return entry.get("examples") or []

    total = violations = 0
    hits = []
    for entry in gold_dne:
        for ft in forbidden_texts(entry):
            total += 1
            n = textnorm.norm_text(ft)
            if n and any(n in s for s in surfaces):
                violations += 1
                hits.append({"forbidden": ft, "kind": entry.get("kind")})
    suppression = 1.0 if total == 0 else (total - violations) / total
    return {"score": suppression, "violations": violations, "total": total, "hits": hits}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_relations_etc.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/stages.py fangan/testcases/harness/tests/test_relations_etc.py
git commit -m "feat(harness): relations, packages, structure, do_not_extract scorers"
```

---

## Task 9: Orchestrator — score one fixture end to end

**Files:**
- Create: `fangan/testcases/harness/scorer.py` (loads YAML, runs all stages, assembles result)
- Test: `fangan/testcases/harness/tests/test_scorer.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_scorer.py`:
```python
import glob, os
import yaml
from harness import scorer

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLDS = sorted(glob.glob(os.path.join(REPO, "fangan/testcases/*/ch*/gold.yaml")))

def test_gold_files_found():
    assert len(GOLDS) == 14

def test_gold_vs_gold_is_perfect():
    # The core sanity invariant: scoring gold against itself yields 100 on every chapter.
    for gp in GOLDS:
        gold = yaml.safe_load(open(gp))
        result = scorer.score_fixture(gold, gold)
        assert result["weighted_score"] == 100.0, f"{gp} -> {result['weighted_score']}"
        for bucket, s in result["stage_scores"].items():
            assert abs(s - 1.0) < 1e-9, f"{gp} bucket {bucket} = {s}"

def test_dropping_an_object_lowers_score():
    gold = yaml.safe_load(open(GOLDS[0]))
    pred = yaml.safe_load(open(GOLDS[0]))
    if pred.get("objects"):
        pred["objects"] = pred["objects"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["weighted_score"] < 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_scorer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.scorer'`.

- [ ] **Step 3: Implement `scorer.score_fixture`**

`fangan/testcases/harness/scorer.py`:
```python
"""End-to-end fixture scorer: runs every stage, reusing one atom alignment."""
from . import align, stages, metrics
from .config import THRESHOLDS


def score_fixture(gold, pred, judge=None):
    gold = gold or {}
    pred = pred or {}

    # 1) atoms first — yields the reusable atom alignment
    atoms = stages.score_atoms(gold.get("evidence_atoms"), pred.get("evidence_atoms"))
    atom_p2g = atoms["alignment"]["p2g"]

    # 2) chunks (via atom map)
    chunks = stages.score_chunks(gold.get("semantic_chunks"), pred.get("semantic_chunks"), atom_p2g)

    # 3) objects (via atom map) -> object alignment
    objects = stages.score_objects(gold.get("objects"), pred.get("objects"), atom_p2g, judge=judge)
    obj_g2p = objects["alignment"]["g2p"]
    obj_p2g = objects["alignment"]["p2g"]

    # 4) packages (via chunk + object alignment)
    packages = stages.score_packages(
        gold.get("context_packages"), pred.get("context_packages"), pred.get("objects"),
        chunk_g2p=chunks["alignment"]["g2p"], obj_g2p=obj_g2p,
    )

    # 5) relations (via object alignment)
    relations = stages.score_relations(gold.get("relations"), pred.get("relations"), obj_g2p, obj_p2g)

    # 6) structure (section_tree + mentions)
    structure = stages.score_structure(
        gold.get("section_tree"), pred.get("section_tree"),
        gold.get("mentions"), pred.get("mentions"), atom_p2g,
    )

    # 7) do_not_extract negative control
    dne = stages.score_do_not_extract(gold.get("do_not_extract"), pred)

    stage_scores = {
        "evidence_atoms": atoms["score"],
        "semantic_chunks": chunks["score"],
        "objects": objects["score"],
        "object_payload": objects["payload"]["f1"],
        "object_evidence": objects["evidence"]["mean_jaccard"],
        "relations": relations["score"],
        "context_packages": packages["score"],
        "do_not_extract": dne["score"],
        "structure": structure["score"],
    }
    weighted = metrics.weighted_total(stage_scores)
    return {
        "weighted_score": weighted,
        "stage_scores": stage_scores,
        "stages": {
            "evidence_atoms": atoms,
            "semantic_chunks": chunks,
            "objects": objects,
            "context_packages": packages,
            "relations": relations,
            "structure": structure,
            "do_not_extract": dne,
        },
        "schema_version": gold.get("schema_version"),
        "profile": (gold.get("source_meta") or {}).get("profile"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_scorer.py -q`
Expected: PASS (3 passed). This is the critical gate — every real gold fixture scores 100 against itself. If any bucket is < 1.0, fix the corresponding stage scorer before proceeding (most likely cause: a real gold field shape the toy tests didn't cover).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/scorer.py fangan/testcases/harness/tests/test_scorer.py
git commit -m "feat(harness): end-to-end fixture scorer (gold-vs-gold == 100)"
```

---

## Task 10: Report emitters (JSON + Markdown)

**Files:**
- Create: `fangan/testcases/harness/report.py`
- Test: `fangan/testcases/harness/tests/test_report.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_report.py`:
```python
import json
from harness import report

RESULT = {
    "weighted_score": 87.5,
    "schema_version": "0.3.3",
    "profile": "article_research",
    "stage_scores": {"evidence_atoms": 0.9, "objects": 0.8},
    "stages": {
        "evidence_atoms": {"score": 0.9, "prf": {"precision": 0.9, "recall": 0.9, "f1": 0.9, "tp": 9, "fp": 1, "fn": 1},
                            "type_accuracy": 1.0, "mean_iou": 0.95,
                            "missed": ["A-X"], "spurious": ["P-Y"], "type_mismatches": []},
        "objects": {"score": 0.8, "prf": {"precision": 0.8, "recall": 0.8, "f1": 0.8, "tp": 8, "fp": 2, "fn": 2},
                     "type_accuracy": 0.9, "payload": {"f1": 0.7, "precision": 0.7, "recall": 0.7, "gaps": []},
                     "evidence": {"mean_jaccard": 0.85},
                     "missed": ["O-Z"], "spurious": [], "type_mismatches": [
                         {"gold_id": "O1", "pred_id": "Z1", "gold_type": "ArticleClaim", "pred_type": "ArticleMethod"}]},
    },
}

def test_to_json_roundtrips():
    s = report.to_json(RESULT)
    assert json.loads(s)["weighted_score"] == 87.5

def test_to_markdown_has_headline_and_sections():
    md = report.to_markdown(RESULT, title="ch00_abstract")
    assert "87.5" in md
    assert "ch00_abstract" in md
    assert "Missed" in md and "A-X" in md          # FN listed
    assert "Type mismatch" in md and "ArticleMethod" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.report'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/report.py`:
```python
"""Render a scorer result as JSON (machine) and Markdown (human/agent feedback)."""
import json


def to_json(result):
    return json.dumps(result, ensure_ascii=False, indent=2, default=list)


def _fmt_pct(x):
    return f"{100.0 * x:.1f}%"


def to_markdown(result, title="(fixture)"):
    lines = []
    lines.append(f"# Score report — {title}")
    lines.append("")
    lines.append(f"**Weighted score: {result['weighted_score']} / 100**  "
                 f"(profile: `{result.get('profile')}`, schema: `{result.get('schema_version')}`)")
    lines.append("")
    lines.append("## Stage scores")
    lines.append("")
    lines.append("| bucket | score |")
    lines.append("| --- | --: |")
    for k, v in result["stage_scores"].items():
        lines.append(f"| {k} | {_fmt_pct(v)} |")
    lines.append("")

    st = result["stages"]

    # Per-stage P/R/F1
    lines.append("## Precision / Recall / F1")
    lines.append("")
    lines.append("| stage | P | R | F1 | extra |")
    lines.append("| --- | --: | --: | --: | --- |")
    for name in ("evidence_atoms", "semantic_chunks", "objects", "relations"):
        s = st.get(name)
        if not s or "prf" not in s:
            continue
        pr = s["prf"]
        extra = ""
        if name == "evidence_atoms":
            extra = f"type_acc={_fmt_pct(s['type_accuracy'])}, mean_iou={s['mean_iou']}"
        elif name == "objects":
            extra = (f"type_acc={_fmt_pct(s['type_accuracy'])}, "
                     f"payload_f1={_fmt_pct(s['payload']['f1'])}, "
                     f"ev_jaccard={s['evidence']['mean_jaccard']}")
        lines.append(f"| {name} | {_fmt_pct(pr['precision'])} | {_fmt_pct(pr['recall'])} "
                     f"| {_fmt_pct(pr['f1'])} | {extra} |")
    lines.append("")

    # Actionable diffs
    def section(header, rows):
        if not rows:
            return
        lines.append(f"## {header}")
        lines.append("")
        for r in rows:
            lines.append(f"- {r}")
        lines.append("")

    for name in ("evidence_atoms", "semantic_chunks", "objects", "relations"):
        s = st.get(name)
        if not s:
            continue
        section(f"Missed in {name} (false negatives)", s.get("missed"))
        section(f"Spurious in {name} (false positives)", s.get("spurious"))
        tms = s.get("type_mismatches") or []
        section(f"Type mismatches in {name}",
                [f"{t['gold_id']} (gold `{t['gold_type']}`) vs {t['pred_id']} (pred `{t['pred_type']}`)"
                 for t in tms])

    # payload gaps
    obj = st.get("objects", {})
    gaps = (obj.get("payload") or {}).get("gaps") or []
    section("Payload-field gaps",
            [f"{g['gold_id']}: missing {g['missing_values']}" for g in gaps])

    # packages
    pkg = st.get("context_packages", {})
    section("Package object-recall misses",
            [f"{d['package']}: {d['missed_expected_objects']}" for d in (pkg.get("details") or [])])

    # do_not_extract
    dne = st.get("do_not_extract", {})
    section("do_not_extract violations (over-extraction)",
            [f"`{h['forbidden']}` ({h.get('kind')})" for h in (dne.get("hits") or [])])

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_report.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/report.py fangan/testcases/harness/tests/test_report.py
git commit -m "feat(harness): JSON + Markdown report emitters"
```

---

## Task 11: Optional LLM judge (off by default)

**Files:**
- Create: `fangan/testcases/harness/judge.py`
- Test: `fangan/testcases/harness/tests/test_judge.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_judge.py`:
```python
from harness import judge

def test_make_judge_none_when_disabled():
    assert judge.make_judge(enabled=False) is None

def test_cached_judge_calls_backend_once():
    calls = []
    def backend(g, p):
        calls.append((g, p))
        return True
    j = judge.CachedJudge(backend)
    assert j("a", "b") is True
    assert j("a", "b") is True
    assert len(calls) == 1  # second call served from cache
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_judge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.judge'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/judge.py`:
```python
"""Optional LLM semantic-equivalence judge. OFF by default (returns None).

A judge is any callable (gold_text, pred_text) -> bool. When enabled without a
custom backend, no real model is wired in (keeps the harness offline/zero-secret):
make_judge returns None unless a backend callable is supplied.
"""
import hashlib


class CachedJudge:
    def __init__(self, backend):
        self._backend = backend
        self._cache = {}

    def _key(self, g, p):
        return hashlib.sha256(f"{g}\x00{p}".encode("utf-8")).hexdigest()

    def __call__(self, gold_text, pred_text):
        k = self._key(gold_text, pred_text)
        if k not in self._cache:
            self._cache[k] = bool(self._backend(gold_text, pred_text))
        return self._cache[k]


def make_judge(enabled=False, backend=None):
    """Return a judge callable or None.

    enabled=False -> None (deterministic mode).
    enabled=True + backend -> CachedJudge(backend).
    enabled=True + no backend -> None (no model wired; caller logs a warning).
    """
    if not enabled or backend is None:
        return None
    return CachedJudge(backend)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_judge.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/judge.py fangan/testcases/harness/tests/test_judge.py
git commit -m "feat(harness): optional cached LLM judge interface (off by default)"
```

---

## Task 12: `score.py` CLI (single chapter)

**Files:**
- Create: `fangan/testcases/harness/score.py`
- Test: `fangan/testcases/harness/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_cli.py`:
```python
import json, os, subprocess, sys

REPO = "/Users/hzf/workspace/silicon_notebook"
TC = os.path.join(REPO, "fangan/testcases")
GOLD_DIR = os.path.join(TC, "engram/ch00_abstract")

def test_cli_gold_vs_itself(tmp_path):
    out_json = tmp_path / "r.json"
    out_md = tmp_path / "r.md"
    cmd = [sys.executable, "-m", "harness.score",
           "--gold", GOLD_DIR,
           "--pred", os.path.join(GOLD_DIR, "gold.yaml"),
           "--out", str(out_json), "--md", str(out_md)]
    proc = subprocess.run(cmd, cwd=TC, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out_json.read_text())
    assert data["weighted_score"] == 100.0
    assert out_md.read_text().strip() != ""
    assert "100.0" in proc.stdout  # headline score echoed to stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_cli.py -q`
Expected: FAIL — `No module named harness.score` (or non-zero return).

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/score.py`:
```python
"""CLI: score one chapter's pred.yaml against its gold.yaml."""
import argparse
import os
import sys

import yaml

from . import scorer, report, judge


def _load_gold(path):
    if os.path.isdir(path):
        path = os.path.join(path, "gold.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f), path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a qiefen extraction against gold.")
    ap.add_argument("--gold", required=True, help="chapter dir or path to gold.yaml")
    ap.add_argument("--pred", required=True, help="path to candidate pred.yaml")
    ap.add_argument("--out", help="write report.json here")
    ap.add_argument("--md", help="write report.md here")
    ap.add_argument("--title", help="title shown in the markdown report")
    ap.add_argument("--llm-judge", action="store_true", help="enable LLM judge (no backend wired by default)")
    args = ap.parse_args(argv)

    gold, gold_path = _load_gold(args.gold)
    with open(args.pred, "r", encoding="utf-8") as f:
        pred = yaml.safe_load(f)

    j = judge.make_judge(enabled=args.llm_judge, backend=None)
    if args.llm_judge and j is None:
        print("warning: --llm-judge set but no backend wired; using deterministic equivalence",
              file=sys.stderr)

    result = scorer.score_fixture(gold, pred, judge=j)
    title = args.title or os.path.basename(os.path.dirname(gold_path)) or "fixture"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report.to_json(result))
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(report.to_markdown(result, title=title))

    print(f"{title}: {result['weighted_score']} / 100")
    for k, v in result["stage_scores"].items():
        print(f"  {k:18s} {100.0 * v:6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_cli.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/score.py fangan/testcases/harness/tests/test_cli.py
git commit -m "feat(harness): score.py single-chapter CLI"
```

---

## Task 13: `run_all.py` (candidate tree → aggregate + leaderboard)

**Files:**
- Create: `fangan/testcases/harness/run_all.py`
- Test: `fangan/testcases/harness/tests/test_run_all.py`

- [ ] **Step 1: Write the failing test**

`fangan/testcases/harness/tests/test_run_all.py`:
```python
import os, json
from harness import run_all

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLD_ROOT = os.path.join(REPO, "fangan/testcases")

def test_run_all_gold_as_candidate_scores_100(tmp_path):
    # Using gold itself as the candidate tree: every chapter must score 100.
    agg = run_all.run(gold_root=GOLD_ROOT, pred_root=GOLD_ROOT, out_dir=str(tmp_path))
    assert agg["chapters_scored"] == 14
    assert abs(agg["mean_weighted_score"] - 100.0) < 1e-9
    assert os.path.exists(os.path.join(str(tmp_path), "aggregate.json"))
    assert os.path.exists(os.path.join(str(tmp_path), "leaderboard.md"))
    # leaderboard lists each chapter
    lb = open(os.path.join(str(tmp_path), "leaderboard.md")).read()
    assert "ch00_abstract" in lb
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_run_all.py -q`
Expected: FAIL — `No module named 'harness.run_all'`.

- [ ] **Step 3: Write implementation**

`fangan/testcases/harness/run_all.py`:
```python
"""Score a candidate tree against all gold chapters; emit aggregate + leaderboard.

A candidate tree mirrors the gold layout: <pred_root>/<doc>/<chapter>/pred.yaml
(when pred_root IS the gold tree, gold.yaml is used as the candidate too).
"""
import argparse
import glob
import os

import yaml

from . import scorer, report


def _candidate_path(pred_root, doc, chapter):
    for name in ("pred.yaml", "gold.yaml"):
        cand = os.path.join(pred_root, doc, chapter, name)
        if os.path.exists(cand):
            return cand
    return None


def run(gold_root, pred_root, out_dir, judge=None):
    golds = sorted(glob.glob(os.path.join(gold_root, "*", "ch*", "gold.yaml")))
    rows = []
    for gp in golds:
        chapter = os.path.basename(os.path.dirname(gp))
        doc = os.path.basename(os.path.dirname(os.path.dirname(gp)))
        cand = _candidate_path(pred_root, doc, chapter)
        gold = yaml.safe_load(open(gp, encoding="utf-8"))
        if cand is None:
            rows.append({"doc": doc, "chapter": chapter, "weighted_score": 0.0,
                         "missing_candidate": True, "stage_scores": {}})
            continue
        pred = yaml.safe_load(open(cand, encoding="utf-8"))
        result = scorer.score_fixture(gold, pred, judge=judge)
        rows.append({"doc": doc, "chapter": chapter,
                     "weighted_score": result["weighted_score"],
                     "stage_scores": result["stage_scores"]})

    scored = [r for r in rows if not r.get("missing_candidate")]
    mean = round(sum(r["weighted_score"] for r in scored) / len(scored), 2) if scored else 0.0
    agg = {"chapters_scored": len(scored), "chapters_total": len(rows),
           "mean_weighted_score": mean, "rows": rows}

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "aggregate.json"), "w", encoding="utf-8") as f:
        f.write(report.to_json(agg))
    with open(os.path.join(out_dir, "leaderboard.md"), "w", encoding="utf-8") as f:
        f.write(_leaderboard_md(agg))
    return agg


def _leaderboard_md(agg):
    lines = [f"# Candidate leaderboard", "",
             f"**Mean weighted score: {agg['mean_weighted_score']} / 100** "
             f"over {agg['chapters_scored']}/{agg['chapters_total']} chapters", "",
             "| doc | chapter | score | atoms | chunks | objects | relations |",
             "| --- | --- | --: | --: | --: | --: | --: |"]
    for r in sorted(agg["rows"], key=lambda x: (x["doc"], x["chapter"])):
        ss = r.get("stage_scores", {})
        def pct(k):
            return f"{100.0 * ss[k]:.0f}%" if k in ss else "-"
        flag = " (missing)" if r.get("missing_candidate") else ""
        lines.append(f"| {r['doc']} | {r['chapter']}{flag} | {r['weighted_score']} "
                     f"| {pct('evidence_atoms')} | {pct('semantic_chunks')} "
                     f"| {pct('objects')} | {pct('relations')} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a candidate tree against all gold chapters.")
    ap.add_argument("--gold-root", default="fangan/testcases")
    ap.add_argument("--pred-root", required=True)
    ap.add_argument("--out-dir", default="harness_out")
    args = ap.parse_args(argv)
    agg = run(args.gold_root, args.pred_root, args.out_dir)
    print(f"mean {agg['mean_weighted_score']} / 100 over {agg['chapters_scored']} chapters "
          f"-> {args.out_dir}/leaderboard.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_run_all.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/run_all.py fangan/testcases/harness/tests/test_run_all.py
git commit -m "feat(harness): run_all aggregate + leaderboard"
```

---

## Task 14: Perturbation integration tests + README

**Files:**
- Create: `fangan/testcases/harness/tests/test_perturbation.py`
- Create: `fangan/testcases/harness/README.md`

- [ ] **Step 1: Write the perturbation tests**

`fangan/testcases/harness/tests/test_perturbation.py`:
```python
import copy, glob, os
import yaml
from harness import scorer

REPO = "/Users/hzf/workspace/silicon_notebook"
# pick a chapter with atoms, chunks, objects, relations all present
GOLD = os.path.join(REPO, "fangan/testcases/engram/ch02_architecture/gold.yaml")

def load():
    g = yaml.safe_load(open(GOLD, encoding="utf-8"))
    return g, copy.deepcopy(g)

def test_shifting_a_span_lowers_atom_iou_and_recall():
    gold, pred = load()
    a = pred["evidence_atoms"][0]["source_span"]
    a["char_start"] = a["char_start"] + 100000  # push it far enough to break overlap
    a["char_end"] = a["char_end"] + 100000
    res = scorer.score_fixture(gold, pred)
    assert res["stage_scores"]["evidence_atoms"] < 1.0

def test_flipping_an_atom_type_lowers_type_accuracy():
    gold, pred = load()
    pred["evidence_atoms"][0]["atom_type"] = "DEFINITELY_WRONG"
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["evidence_atoms"]["type_accuracy"] < 1.0

def test_injecting_spurious_object_lowers_object_precision():
    gold, pred = load()
    pred["objects"].append({"id": "JUNK", "type": "ArticleClaim", "home_package": "PKG-NONE",
                            "local_evidence_atom_ids": [], "supporting_context_atom_ids": [],
                            "payload": {"statement": "totally unrelated fabricated claim xyz"}})
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["objects"]["prf"]["precision"] < 1.0

def test_dropping_a_relation_lowers_relation_recall():
    gold, pred = load()
    pred["relations"] = pred["relations"][:-1]
    res = scorer.score_fixture(gold, pred)
    assert res["stages"]["relations"]["prf"]["recall"] < 1.0

def test_extracting_forbidden_text_triggers_violation():
    gold, pred = load()
    dne = (gold.get("do_not_extract") or [])
    forbidden = None
    for e in dne:
        forbidden = e.get("text") or (e.get("examples") or [None])[0]
        if forbidden:
            break
    if forbidden:
        pred.setdefault("mentions", []).append(
            {"id": "BAD", "text": forbidden, "type": "Concept", "atom_id": pred["evidence_atoms"][0]["id"]})
        res = scorer.score_fixture(gold, pred)
        assert res["stages"]["do_not_extract"]["violations"] >= 1
```

- [ ] **Step 2: Run the perturbation tests**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_perturbation.py -q`
Expected: PASS (5 passed). If `ch02_architecture` lacks `do_not_extract` with a usable text, the last test no-ops safely (guarded by `if forbidden`).

- [ ] **Step 3: Write the README**

`fangan/testcases/harness/README.md`:
```markdown
# qiefen 抽取评分 Harness

把 agent 生成的抽取结果（`pred.yaml`，与 `gold.yaml` 同 schema）与金标准逐 stage 对比，
给出每 stage 的 P/R/F1、一个 0–100 的加权总分，以及可操作的差异报告。

## 运行（从 `fangan/testcases/` 目录）

单章：
    python -m harness.score --gold engram/ch00_abstract --pred path/to/pred.yaml \
        --out report.json --md report.md

全量（候选目录镜像 `engram/chXX/ cmos/chXX/`，每章一个 `pred.yaml`）：
    python -m harness.run_all --gold-root . --pred-root /path/to/candidate --out-dir out

自检（gold-vs-gold 必须每章 100）：
    python -m pytest harness/ -q

## 评分模型

- **先对齐 atoms**（`source_span` IoU）→ 复用 `gold_atom_id↔pred_atom_id` 映射到下游。
- 每 stage：loose 对齐（按内容）给诊断，strict TP（内容+类型）算 F1。
- 加权总分权重见 `config.py`（唯一调参入口）。
- `--llm-judge` 可选启用语义等价（默认关闭、纯确定性、零密钥；需自行注入 backend）。

## 产出

- `report.json`：机器可读，全 stage 指标 + 匹配/未匹配 id。
- `report.md`：标题分、stage 表、漏报/误报/类型错配/payload 缺失/过抽取分区——喂回 agent 迭代。
- `run_all` 另出 `aggregate.json` + `leaderboard.md`。
```

- [ ] **Step 4: Run the full suite**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/ -q`
Expected: PASS (all tests green, ~40 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/tests/test_perturbation.py fangan/testcases/harness/README.md
git commit -m "test(harness): perturbation integration tests + README"
```

---

## Task 15: Demonstrate the feedback loop on a degraded candidate

**Files:**
- Create: `fangan/testcases/harness/tests/test_demo_feedback.py` (sanity that the markdown diff is non-empty and actionable on an imperfect candidate)

- [ ] **Step 1: Write the test**

`fangan/testcases/harness/tests/test_demo_feedback.py`:
```python
import copy, os
import yaml
from harness import scorer, report

REPO = "/Users/hzf/workspace/silicon_notebook"
GOLD = os.path.join(REPO, "fangan/testcases/engram/ch00_abstract/gold.yaml")

def test_imperfect_candidate_produces_actionable_markdown():
    gold = yaml.safe_load(open(GOLD, encoding="utf-8"))
    pred = copy.deepcopy(gold)
    # degrade: drop one object, drop one atom, flip one relation type
    pred["objects"] = pred["objects"][:-1]
    pred["evidence_atoms"] = pred["evidence_atoms"][:-1]
    if pred.get("relations"):
        pred["relations"][0]["relation_type"] = "WRONG_REL_TYPE"
    result = scorer.score_fixture(gold, pred)
    md = report.to_markdown(result, title="degraded-demo")

    assert result["weighted_score"] < 100.0
    # the report must name at least one concrete missed/spurious/type-mismatch item
    assert ("false negatives" in md) or ("Type mismatches" in md)
    # machine report must be JSON-serializable
    assert report.to_json(result).startswith("{")
```

- [ ] **Step 2: Run test to verify behavior**

Run: `cd /Users/hzf/workspace/silicon_notebook/fangan/testcases && python -m pytest harness/tests/test_demo_feedback.py -q`
Expected: PASS (1 passed).

- [ ] **Step 3: Manual smoke — eyeball a real report**

Run:
```bash
cd /Users/hzf/workspace/silicon_notebook/fangan/testcases
python -m harness.score --gold engram/ch00_abstract --pred engram/ch00_abstract/gold.yaml --md /tmp/r.md
sed -n '1,40p' /tmp/r.md
```
Expected: headline `100.0 / 100`, stage table all `100.0%`, no diff sections (perfect candidate). Confirms the human-readable path renders.

- [ ] **Step 4: Commit**

```bash
cd /Users/hzf/workspace/silicon_notebook
git add fangan/testcases/harness/tests/test_demo_feedback.py
git commit -m "test(harness): feedback-loop demo on degraded candidate"
```

---

## Self-Review

**Spec coverage:**
- Same-schema candidate input → loaders in `score.py`/`run_all.py` ✓ (Tasks 12–13)
- Hybrid matching (deterministic core + optional judge) → `align.py` deterministic, `judge.py` optional, threaded into `score_objects`/payload ✓ (Tasks 4–7, 11)
- Atom-alignment-first reused downstream → `scorer.score_fixture` order ✓ (Task 9)
- All 8 stages scored (section_tree, atoms, chunks, packages, objects, relations, mentions, do_not_extract) → Tasks 5–8 (mentions folded into `structure`) ✓
- Per-stage P/R/F1 + weighted 0–100 → `metrics.weighted_total`, report table ✓
- JSON + Markdown dual output with actionable sections → `report.py` ✓ (Task 10)
- `run_all` aggregate + leaderboard ✓ (Task 13)
- gold-vs-gold == 100 + perturbation tests covering both profiles → Tasks 9, 14 (engram + ch02; add a cmos chapter check below) 
- LLM judge off by default, zero secrets → `make_judge(enabled=False)→None` ✓

**Gap found & fixed:** spec §11 requires the self-test to cover **both** profiles. Task 9's `test_gold_vs_gold_is_perfect` already iterates **all 14** gold files (9 engram + 5 cmos), so both profiles are covered by the 100-invariant. No extra task needed; noting it here.

**Placeholder scan:** no TBD/TODO; every code step contains complete, runnable code.

**Type consistency:** Alignment dict keys (`matches/g2p/p2g/unmatched_gold/unmatched_pred`) are produced by `align.greedy` and consumed identically in `stages.py`/`scorer.py`. Stage result dicts always carry `score` + (where applicable) `prf`, consumed uniformly by `report.py` and `scorer.weighted_total` mapping. `score_objects` exposes `payload.f1` and `evidence.mean_jaccard`, which `scorer.score_fixture` maps to the `object_payload`/`object_evidence` buckets — names match.

**Risk note (for the executor):** Task 9's gold-vs-gold test is the real gate. If a stage scorer is < 1.0 on a real fixture, the most likely causes are: (a) a gold payload uses a value shape `payload_values` mishandles (e.g., numbers — handled via `str()`), (b) `do_not_extract` `pattern` entries with no `examples` (→ 0 forbidden texts → suppression 1.0, fine), or (c) chunk over/under-split heuristic firing on equal sets (guarded by strict `<`). Fix the scorer, not the gold.
