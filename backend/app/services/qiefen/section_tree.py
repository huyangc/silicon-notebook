"""S2: heading elements -> SectionNode list with breadcrumb `path`.

Gold normalizes paths to the NUMERIC label chain, not the heading prose:
  "# 2. Architecture"        -> "2"
  "# 2.1. Overview"          -> "2 > 2.1"
  "# Chapter 1 ..."          -> "1"
  "# 1.1 Analog ..."         -> "1 > 1.1"
  "# Abstract"               -> "Abstract"          (unnumbered, no chapter yet)
  "# PROBLEMS" (under ch.1)  -> "1 > ... > PROBLEMS" (unnumbered -> current chain)
Numbering drives nesting because MinerU emits every heading at level 1 (`#`),
so the markdown level cannot be trusted. Section paths are scored as a set of
normalized strings (harness `score_structure`), so matching the numeric chain
of the numbered sections — the large majority of gold nodes — is what counts.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.services.qiefen.models import SectionNode, SourceElementQ

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# Leading numeric label, optionally prefixed by Chapter/Section. Captures the
# dotted number ("2", "2.1", "1.1"); a trailing dot ("2.1.") is not captured.
_NUM = re.compile(r"^(?:chapter|section)?\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)


def _heading_title(el: SourceElementQ) -> str:
    m = _HEADING.match(el.text)
    return m.group(2).strip() if m else el.text.strip()


def _numeric_label(title: str) -> Optional[str]:
    m = _NUM.match(title)
    return m.group(1) if m else None


def build_section_tree(elements: List[SourceElementQ]) -> List[SectionNode]:
    nodes: List[SectionNode] = []
    counter = 0
    cur_chain: List[str] = []  # numeric prefix chain of the last numbered heading
    for el in elements:
        if el.type != "heading":
            continue
        title = _heading_title(el)
        num = _numeric_label(title)
        if num:
            parts = num.split(".")
            chain = [".".join(parts[: i + 1]) for i in range(len(parts))]
            path = " > ".join(chain)
            cur_chain = chain
        elif cur_chain:
            path = " > ".join(cur_chain + [title])
        else:
            path = title
        counter += 1
        nodes.append(SectionNode(id=f"SEC-{counter}", path=path, title=title))
    return nodes
