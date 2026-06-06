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
