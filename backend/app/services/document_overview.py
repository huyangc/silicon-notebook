"""Conservative overview intent and source resolution, independent of models."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OverviewIntent:
    kind: str
    title: str = ""


_QUOTED = re.compile(r"《([^》]+)》|「([^」]+)」|[\"“]([^\"”]+)[\"”]")
_CN_DOCUMENT = r"(?:文档|文章|论文|资料)"
_CN_SCOPE = r"(?:(?:这个|当前|本|该)?(?:库|笔记本)(?:中|里|内)?的?)"
_CN_SOURCE = rf"(?:{_CN_SCOPE})?(?:(?:这|那|该)(?:篇|份){_CN_DOCUMENT}?|该{_CN_DOCUMENT}|{_CN_DOCUMENT}|@title@|{_CN_DOCUMENT}@title@|@title@这篇{_CN_DOCUMENT})"
_CN_CATALOG = rf"(?:(?:{_CN_SCOPE})(?:(?:所有|全部)的?)?{_CN_DOCUMENT}|(?:每篇|各篇|所有|全部|这些)(?:的)?{_CN_DOCUMENT}|{_CN_DOCUMENT}(?=分别|各自))"
_EN_DOCUMENT = r"(?:document|paper|article|file)"
_EN_SCOPE = r"(?:in (?:this|the|my|our) (?:library|notebook))"
_EN_SOURCE = rf"(?:(?:this|that|the) {_EN_DOCUMENT}|(?:the )?{_EN_DOCUMENT} @title@|@title@)"
_EN_CATALOG = rf"(?:(?:each|every) {_EN_DOCUMENT}|(?:all(?: the)?|the|these) {_EN_DOCUMENT}s|{_EN_DOCUMENT}s)(?: {_EN_SCOPE})?"


def _whole_document_request(question: str, subject: str, *, english: bool) -> bool:
    """Match complete subject/action templates, leaving any topic modifiers ranked."""
    if english:
        patterns = (
            rf"(?:please )?(?:briefly )?(?:summari[sz]e|introduce) {subject}",
            rf"(?:please )?(?:give|provide)(?: me)? (?:a |an )?(?:brief )?(?:summary|overview|introduction) of {subject}",
            rf"(?:a |an )?(?:summary|overview|introduction) of {subject}",
            rf"what (?:is|are) {subject} about",
            rf"what (?:does|do) {subject} (?:cover|discuss|describe)",
        )
    else:
        patterns = (
            rf"(?:请|请帮我|帮我)?(?:分别|逐篇)?(?:简单|简要)?(?:介绍|概述|概括|总结)(?:一下)?{subject}(?:的(?:主要内容|内容|主题))?",
            rf"{subject}(?:分别|各自)?(?:主要)?(?:介绍|讲|讲述|讲的|包含)(?:了)?(?:什么|哪些内容|什么内容)",
            rf"{subject}的(?:主要内容|内容|主题)(?:是|有)什么",
        )
    return any(re.fullmatch(pattern, question, re.I) for pattern in patterns)


def overview_intent(question: str) -> OverviewIntent | None:
    """Only route explicit document introductions; topical questions stay ranked.

    The raw question alone controls this path. A generated rewrite cannot
    broaden its source scope or change a fact question into an overview.
    """
    q = " ".join(question.strip().split()).rstrip("?？!！。.")
    # One-source preparation must never silently drop another named target.
    # Preserve the ordinary multi-query lane for explicit multi-document asks.
    if len(_QUOTED.findall(q)) > 1:
        return None
    quoted = _QUOTED.search(q)
    title = next((part for part in quoted.groups() if part), "") if quoted else ""
    if quoted:
        # Quotes become a title only in document-subject position. Bare ordinary
        # quotes may denote a topic; only book-title brackets stand alone.
        q = _QUOTED.sub("@title@", q)
    cn = q.replace(" ", "")
    cn_source = _CN_SOURCE
    en_source = _EN_SOURCE
    if quoted and not quoted[0].startswith("《"):
        cn_source = rf"(?:{_CN_DOCUMENT}@title@|@title@这篇{_CN_DOCUMENT})"
        en_source = rf"(?:the )?{_EN_DOCUMENT} @title@"
    if _whole_document_request(cn, _CN_CATALOG, english=False) or _whole_document_request(q, _EN_CATALOG, english=True):
        return OverviewIntent("catalog")
    if _whole_document_request(cn, cn_source, english=False) or _whole_document_request(q, en_source, english=True):
        return OverviewIntent("source", title)
    # Enumeration only accepts an unfiltered directory subject, never "papers
    # about X" or "which documents cover X".
    if re.fullmatch(rf"(?:请)?列出(?:{_CN_CATALOG}|{_CN_DOCUMENT})|(?:{_CN_SCOPE})?有哪些{_CN_DOCUMENT}", cn):
        return OverviewIntent("catalog")
    if re.fullmatch(rf"(?:please )?list {_EN_CATALOG}|(?:what|which) {_EN_DOCUMENT}s are {_EN_SCOPE}", q, re.I):
        return OverviewIntent("catalog")
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
