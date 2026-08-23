"""Frozen source display-title contract.

One rule with one definition point: a grounded paper title outranks the name
the file was uploaded under; every other source keeps its ordinary source
title, then its file name.

Three call sites name sources for the user, and they name the *same* sources in
the same session — the answer's ``[k]`` citation cards
(``evidence_context.EvidenceContextService.citation_titles``), the element
candidates the reasoning retriever scores and renders as evidence
(``retrieval_candidates``), and the typed-collection list
(``collection_enumeration``).  Each arrived separately and each wrote the rule
out again; the failure that costs is not the duplication but its drift, which
is visible to the user in one screen: the same paper listed as "1706.03762.pdf"
on a list card and as its parsed title on the citation card next to it.

A leaf on purpose.  It imports nothing from the application, so the citation
path, the retrieval path and the enumeration path each depend on it without
depending on one another.

``test_source_display_title.py`` holds the source-level guard that keeps this
the only copy (its ``DEFINITION_SITE`` points here after the B3 move;
``app.services.source_display`` re-exports both names unchanged so the three
existing display-path importers — evidence_context, retrieval_candidates,
collection_enumeration — are untouched, and app.repositories can import
``summary_display_title`` directly).
"""
from __future__ import annotations

from typing import Any


def _columns(row: Any, *keys: str) -> dict[str, Any]:
    """Read the named columns off a source row, whatever shape it arrives in.

    Subscript rather than ``.get``: ``source_metadata`` returns plain dicts but
    ``source_display_rows`` hands back driver rows on the caller's connection,
    and ``sqlite3.Row`` has no ``.get``.  A column the row does not carry reads
    as ``None`` instead of raising — the citation path passes ``{}`` for a
    source whose metadata row was not found, and a source with no name is a
    display detail, not an error.
    """
    values: dict[str, Any] = {}
    for key in keys:
        try:
            values[key] = row[key]
        except (KeyError, IndexError, TypeError):
            values[key] = None
    return values


def source_display_title(row: Any) -> str:
    """The user-facing title of one source row, or ``""`` if it has no name.

    The parsed paper title wins only when the row is marked as a paper *and*
    that title is nonblank: ``is_paper`` alone can sit on a row whose title
    extraction was rejected by the grounding check, and a whitespace-only title
    is not a title.  Callers that must not show an unnamed source test the
    empty string; nothing here invents a placeholder.

    The ordinary branch picks before it strips, so a whitespace-only ``title``
    shadows ``file_name`` and yields ``""``.  That is what all three call sites
    did before they shared this function and it is pinned by
    ``test_whitespace_only_source_title_still_beats_the_file_name``: changing it
    is a user-visible behaviour change and belongs to its own change, not to
    the consolidation.
    """
    values = _columns(row, "is_paper", "paper_title", "title", "file_name")
    paper_title = str(values["paper_title"] or "").strip()
    if values["is_paper"] and paper_title:
        return paper_title
    return str(values["title"] or values["file_name"] or "").strip()


def summary_display_title(row: Any, meta: "dict | None") -> str:
    """``SourceSummary.display_title`` 的取数管道——**不是**规则本身。

    四个构造点(两个后端 × 单条/批量)拿到的是两个载体:sources 行本身,加上
    ``paper_meta_for_sources`` 单独水合出来的字典(该源没有 paper meta 行时为
    None)。这里把后者**覆盖**到前者上再委托给 ``source_display_title``,让那
    一个函数继续是规则的唯一定义点。

    刻意不在这里逐个点名 ``title`` / ``file_name``:那两列本函数一个字都不该
    懂——懂了就是第二份实现的开始,``test_source_display_title.py`` 的守卫也正是
    按「同作用域读齐 is_paper/paper_title/file_name」识别这件事的。这里只负责
    论文元数据那两列的**归位**,其余原样透传。

    ``dict(row)`` 覆盖 sqlite3.Row 与 psycopg 字典行两种形状;两者都支持按键
    迭代,而 sqlite3.Row 没有 ``.get``(所以不能用 ``{**row}`` 之外的捷径)。
    """
    values = dict(row)
    values["is_paper"] = bool(meta["is_paper"]) if meta else False
    values["paper_title"] = (meta["paper_title"] if meta else "") or ""
    return source_display_title(values)
