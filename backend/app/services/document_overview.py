"""Conservative overview intent and source resolution, independent of models."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OverviewIntent:
    kind: str
    title: str = ""


_OVERVIEW = re.compile(
    r"介绍|概述|概览|总结|概括|讲了什么|讲什么|讲的什么|主要内容|哪些内容|什么内容"
    r"|\b(?:summari[sz]e|summary|overview|introduce|what.*(?:about|cover))\b", re.I,
)
_DOCUMENT = re.compile(r"文档|文章|论文|资料|这篇|该篇|\b(?:documents?|papers?|articles?|files?)\b", re.I)
_CATALOG = re.compile(
    r"(?:库|笔记本).*(?:文档|文章|论文|资料)|(?:每篇|逐篇|各篇|分别|所有|全部).*(?:文档|文章|论文)"
    r"|(?:文档|文章|论文).*(?:分别|各自)|\b(?:each|every|all)\b.*\b(?:documents?|papers?|articles?|files?)\b"
    r"|\b(?:library|notebook)\b.*\b(?:documents?|papers?|articles?|files?)\b", re.I,
)
_LIST = re.compile(r"有哪些|哪几篇|列出|清单|目录|\b(?:list|which)\b", re.I)
_QUOTED = re.compile(r"《([^》]+)》|「([^」]+)」|[\"“]([^\"”]+)[\"”]")


def overview_intent(question: str) -> OverviewIntent | None:
    """Only route explicit document introductions; topical questions stay ranked.

    The raw question alone controls this path. A generated rewrite cannot
    broaden its source scope or change a fact question into an overview.
    """
    q = question.strip()
    if re.search(r"比较|对比|诊断|设计|评审|\b(?:compare|contrast|diagnose|design|review)\b", q, re.I):
        return None
    if not (_OVERVIEW.search(q) or (_LIST.search(q) and _DOCUMENT.search(q))):
        return None
    # One-source preparation must never silently drop another named target.
    # Preserve the ordinary multi-query lane for explicit multi-document asks.
    if len(_QUOTED.findall(q)) > 1:
        return None
    # "Summarize the methods in this paper" asks for a topic, not a whole
    # document. The existing relevance path retains that topic as its query.
    if re.search(r"\b(?:in|from)\s+(?:this|that|the)\s+(?:document|paper|article|file)\b", q, re.I):
        return None
    quoted = _QUOTED.search(q)
    is_document_title = quoted and (quoted[0].startswith("《") or _DOCUMENT.search(q))
    topic_probe = _QUOTED.sub("文档", q) if is_document_title else q
    if re.search(
        r"(?:文档|文章|论文)(?:中|里|内)|(?:文档|文章|论文)的(?!主要内容|内容|主题|概述|摘要|总结)"
        r"|\b(?:document|paper|article)(?:'s|’s)\s+(?!content|summary|overview)",
        topic_probe, re.I,
    ):
        return None
    if re.search(r"(?:文档|文章|论文)(?:介绍|包含|列出)(?:了)?(?:哪些|什么)(?!内容|主题)", topic_probe):
        return None
    if is_document_title and _OVERVIEW.search(q):
        return OverviewIntent("source", next(part for part in quoted.groups() if part))
    if re.search(r"这篇|该篇|这份|该文档|这篇文章|\b(?:this|that)\s+(?:document|paper|article|file)\b", q, re.I):
        return OverviewIntent("source")
    if _CATALOG.search(q):
        return OverviewIntent("catalog")
    if _DOCUMENT.search(q):
        return OverviewIntent("source")
    return None


def resolve_overview_source(intent: OverviewIntent, catalog, question: str):
    """Prove uniqueness over a complete authorized directory, never its prefix."""
    if not catalog.result_sets or not catalog.result_sets[0].coverage.complete:
        return None, "当前文档目录尚未完整读取，请在来源面板仅选择目标文档后重试。"
    items = catalog.items
    if intent.title:
        normalize = lambda text: "".join(text.split()).casefold()
        matches = [item for item in items if normalize(item.source_title) == normalize(intent.title)]
    else:
        matches = [item for item in items if item.source_title and item.source_title in question]
        if not matches and len(items) == 1:
            matches = items
    if len(matches) == 1:
        return matches[0], ""
    if not matches and intent.title:
        return None, "当前选择范围内未找到该标题的文档，请核对标题或在来源面板选择目标文档。"
    return None, "请在来源面板仅选择要介绍的文档，或用《完整文档标题》明确指定；同名文档需要先收窄来源范围。"
