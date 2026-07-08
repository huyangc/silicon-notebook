"""Mention-bridge 匹配核:别名表构建 + 命中校验。纯函数、零 IO。

trigram FTS 召回候选后,Latin 别名必须过 \\b 词边界后校验(trigram 是子串
语义,rope 会命中 europe);CJK 无词边界概念,子串即命中。"""
from __future__ import annotations
import re
from typing import Dict, List, Set, Tuple

_PAREN_ACRONYM_RE = re.compile(r"^(.*\S)\s*\(([^)]+)\)\s*$")
_ACR_RE = re.compile(r"^[A-Za-z0-9]{3,8}$")
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def is_latin(alias: str) -> bool:
    return bool(_ASCII_RE.match(alias))


def _long_enough(alias: str, latin_min: int, cjk_min: int) -> bool:
    return len(alias) >= (latin_min if is_latin(alias) else cjk_min)


def build_alias_table(clusters: List[Tuple[str, str]], *, latin_min: int = 4,
                      cjk_min: int = 3) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for cid, name in clusters:
        nm = (name or "").strip()
        gated, exempt = set(), set()
        if nm:
            gated.add(nm.lower())
            m = _PAREN_ACRONYM_RE.match(nm)
            if m:
                head, acr = m.group(1).strip(), m.group(2).strip()
                gated.add(head.lower())
                if _ACR_RE.match(acr):
                    # 括号缩写绕过 latin_min:显式 "(ACR)" 模式 precision 高,
                    # GQA/MQA/SFT 等 3 位缩写是共提桥最有价值的别名;
                    # 长度下限由 _ACR_RE 的 {3,8} 承担(trigram 最短查询=3)。
                    exempt.add(acr.lower())
        kept = {a for a in gated if _long_enough(a, latin_min, cjk_min)} | exempt
        if kept:
            out[cid] = kept
    return out


def boundary_hit(alias: str, text_lower: str) -> bool:
    if is_latin(alias):
        return re.search(r"\b" + re.escape(alias) + r"\b", text_lower) is not None
    return alias in text_lower
