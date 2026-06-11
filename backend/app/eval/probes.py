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


_VERB_RE = re.compile(
    r"\b(is|are|was|were|be|been|has|have|had|can|cannot|will|provides?|requires?|"
    r"causes?|increases?|reduces?|decreases?|achieves?|applies|operates?|"
    r"depends?|uses?|results?|produces?|equals?|yields?|improves?|limits?|"
    r"exhibits?|understands?|exists?|comprises?|presents?|deals?|serves?|draws?|"
    r"forms?|employs?|includes?|defines?|addresses|combines?|enhances?|consumes?|"
    r"involves?|remains?|holds?|corrects?|opposes?|impacts?|integrates?|works?|"
    r"organizes?|distinguishes?|compensates?|attenuates?|distorts?|exceeds?|"
    r"affects?|enables?|allows?|generates?|introduces?|represents?|behaves?|"
    r"flows?|drives?|sets?|makes?|takes?|gives?|shows?|needs?|means?|occurs?|"
    r"consists?|contains?|maintains?|determines?|describes?|"
    r"finds?|performs?|demands?|proves?|senses?|processes|process|extracts?|"
    r"handles?|supports?|realizes?|implements?|derives?|relates?|varies|vary|"
    r"scales?|dominates?|tends?|measures?|controls?|converts?|amplifies|amplify|"
    r"biases|couples?|filters?|samples?|stores?|computes?|assumes?|suffers?|"
    r"avoids?|prevents?|ensures?|exploits?|constitutes?|characterizes?|"
    r"illustrates?|demonstrates?|indicates?|suggests?|implies|imply|refers?|"
    r"denotes?|consumes?|emerges?|arises?|leads?|enable|model|models|"
    r"become(s)?|became|becoming|continue(s|d)?|detect(s|ed)?|activate(s|d)?"
    r"|fall(s)?|fell|converted|filtered|adjust(s|ed)?|delay(s|ed)?"
    r"|counter(s|ed)?|anticipate(s|d)?)\b", re.I)

_META_RE = re.compile(
    r"\b(this (chapter|book|text|section|paper)|next chapter|chapter \d|section \d|"
    r"i wanted|we will|in this (chapter|book|text|section))\b", re.I)


def claim_degraded(name: str) -> bool:
    n = (name or "").strip()
    words = n.split()
    if len(words) < 4:
        return True
    if _META_RE.search(n):     # 元叙述/章节导航/前言,非 truth-evaluable 技术断言
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
