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
