"""Deterministic ranking cards, counted evidence and disclosures (design §6).

Counts describe distinct owner sources (§4 / §13.1); a registered citation
shows one mention and does not serve as proof of the count.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from app.domain.concept_ranking_contracts import (
    CONCEPT_RANK_CITATIONS_PER_ROW,
    CONCEPT_RANK_INLINE_ROWS_PER_RUN,
    RANK_FAILURE_REASONS,
)
from app.models.ask import (
    Citation,
    ConceptRankingCoverage,
    ConceptRankingResult,
    ConceptRankingRow,
    ConceptRankingSection,
    RankingFailure,
)
from app.services.collection_enumeration_answer import CollectionKeyAllocator
from app.services.concept_ranking_paging import rank_next_cursor, rank_page_slices
from app.services.context_spans import entry_lines, joined

_FAILURE_LINES = {
    "scoped_large_notebook": "没能按提及次数排行：本笔记本内容较多，只勾选部分来源时不支持这类统计。",
    "not_computed": "没能按提及次数排行：本笔记本还没有排行数据。",
    "analysis_unavailable": "没能按提及次数排行：本笔记本暂时不支持这类统计。",
    "refresh_produced_nothing": "没能按提及次数排行：这次更新没有产出排行数据。",
    "recount_budget_exhausted": "没能按提及次数排行：统计超时。",
    "rank_failed": "没能按提及次数排行：统计时出错。",
}
_FAILURE_SUFFIX = "下面的回答只基于检索到的部分内容，不是统计结果。"
_INSTRUCTION = (
    "Counts are computed by the system as the number of distinct sources that "
    "mention each concept; quote them exactly; rows sharing a same_name_group "
    "may be one concept, do not add them; the key cites one place where the "
    "concept is mentioned, not the count."
)
_GAP_FIELDS = ("drifted", "candidates_lexical_only", "query", "stale_counts",
               "recount_capped", "unnamed_dropped")
_FAILED_INSTRUCTION = (
    "Counting did not complete; do not infer which items are mentioned most "
    "from retrieved samples, and tell the user it could not be counted and why."
)
_EMPTY_INSTRUCTION = (
    "No concept in scope matched this request; tell the user no matching concepts "
    "were found (this is a result, not a failure)."
)


@dataclass(frozen=True)
class RankCitationItem:
    object_id: str
    object_type: str
    name: str
    section_path: str
    notebook_id: str
    tier: str
    evidence_element_ids: tuple[str, ...]


@dataclass(frozen=True)
class RankingPreview:
    text: str
    evidence_by_id: dict
    inlined_fingerprints: frozenset[str]


def rank_citation_item_id(notebook_id: str, canonical_id: str, index: int) -> str:
    return f"rank:{notebook_id}:{canonical_id}:{index}"


def _page_libraries(page):
    return tuple((library.coverage, library.rows[window]) for library, window in zip(
        page.computation.libraries,
        rank_page_slices(page.computation, page.page_number, page.page_size)))


def _card_libraries(outcome):
    """Keep the exact page observations; later participant quotas cannot recut them."""
    groups = {}
    coverages = {}
    for page in outcome.successful_pages:
        for coverage, rows in _page_libraries(page):
            if coverage.status == "ok" and not coverage.skipped:
                coverages[coverage.notebook_id] = coverage
    for page in sorted(outcome.successful_pages, key=lambda item: item.page_number):
        for coverage, rows in _page_libraries(page):
            if coverage.status == "ok" and not coverage.skipped:
                group = groups.setdefault(coverage.notebook_id, {})
                for row in rows:
                    group.setdefault(row.canonical_id, row)
    for library in outcome.computation.libraries:
        notebook_id = library.coverage.notebook_id
        if notebook_id not in groups or not groups[notebook_id]:
            groups.setdefault(notebook_id, {})
            coverages[notebook_id] = library.coverage
    return tuple((coverages[notebook_id], tuple(rows.values())) for notebook_id, rows in groups.items())


def _inline_libraries(outcome):
    by_notebook = {}
    for page in outcome.successful_pages:
        for coverage, rows in _page_libraries(page):
            if coverage.status == "ok" and not coverage.skipped:
                by_notebook[coverage.notebook_id] = (coverage, rows)
    libraries = list(by_notebook.values())
    retained = {coverage for coverage, _rows in libraries}
    for library in outcome.computation.libraries:
        coverage = library.coverage
        if coverage not in retained and (coverage.notebook_id not in by_notebook
                                        or coverage.status != "ok" or coverage.skipped):
            libraries.append((coverage, ()))
    return tuple(libraries)


def _latest_outcomes(outcomes):
    """One chain keeps its latest state in its original first-seen position."""
    return tuple({outcome.fingerprint: outcome for outcome in outcomes}.values())


def rank_citation_items(outcomes) -> list[RankCitationItem]:
    """Hand every fetched locator to the existing attestation boundary."""
    items = {}
    for outcome in _latest_outcomes(outcomes):
        for _coverage, rows in _card_libraries(outcome):
            for row in rows:
                for index, evidence in enumerate(row.evidence[:CONCEPT_RANK_CITATIONS_PER_ROW]):
                    item_id = rank_citation_item_id(row.notebook_id, row.canonical_id, index)
                    items[item_id] = RankCitationItem(
                        item_id, "concept", row.name, "", row.notebook_id, row.tier,
                        (evidence.element_id,),
                    )
    return list(items.values())


def _next_cursor(outcome) -> str:
    if not outcome.successful_pages:
        return ""
    page = outcome.successful_pages[-1]
    return rank_next_cursor(page.computation, page.page_number, page.page_size)


def _failure_line(reason: str, count: int = 1, *, suffix: bool = True) -> str:
    if reason == "library_skipped":
        return f"有 {count} 个笔记本读取失败，没有计入排行。"
    line = _FAILURE_LINES.get(reason, "")
    return line + (_FAILURE_SUFFIX if suffix and line else "")


def ranking_result_sets(outcomes, citations: Mapping[str, Citation]) -> list[ConceptRankingResult]:
    results = []
    for outcome in _latest_outcomes(outcomes):
        sections = []
        for coverage, retained_rows in _card_libraries(outcome):
            rows = []
            if coverage.status == "ok" and not coverage.skipped:
                for row in retained_rows:
                    refs = [citations[key] for index in range(len(row.evidence))
                            if (key := rank_citation_item_id(row.notebook_id, row.canonical_id, index))
                            in citations][:CONCEPT_RANK_CITATIONS_PER_ROW]
                    rows.append(ConceptRankingRow(
                        name=row.name, source_count=row.source_count, object_count=row.object_count,
                        same_name_group=row.same_name_group, notebook_id=row.notebook_id,
                        tier=row.tier, citations=refs,
                    ))
            sections.append(ConceptRankingSection(
                coverage=ConceptRankingCoverage(**asdict(coverage), next_cursor=_next_cursor(outcome)),
                rows=rows,
                notice=_failure_line("library_skipped" if coverage.skipped else coverage.reason,
                                     suffix=False) if coverage.status != "ok" or coverage.skipped else "",
            ))
        results.append(ConceptRankingResult(query=outcome.query, scope=outcome.scope, sections=sections))
    return results


def _gaps(coverage, query_candidates: int | None) -> dict[str, str]:
    gaps = {}
    if coverage.drifted:
        gaps["drifted"] = "统计期间来源有变化"
    if coverage.candidates_lexical_only:
        gaps["candidates_lexical_only"] = "所选来源范围内只按字面匹配找候选，同义说法可能漏掉"
    if coverage.path == "query":
        qualifier = f" {query_candidates} 个" if query_candidates is not None else "候选"
        gaps["query"] = f"只在与主题最相关的{qualifier}概念里排行"
    if coverage.stale_counts:
        gaps["stale_counts"] = f"统计建于较早的图谱状态（之后有 {coverage.seq_behind} 次变更未计入）"
    if coverage.recount_capped:
        gaps["recount_capped"] = f"有 {coverage.recount_skipped} 个候选概念因统计量上限没有计入"
    if coverage.unnamed_dropped:
        gaps["unnamed_dropped"] = f"有 {coverage.unnamed_dropped} 个概念取不到名称、没有列出"
    return gaps


def _disclosure(coverage, query_candidates: int | None) -> str:
    return _combined_disclosure((coverage,), query_candidates)


def _combined_disclosure(coverages, query_candidates: int | None) -> str:
    """One library's observations, without adding or discarding distinct counts."""
    if all(coverage.exact for coverage in coverages):
        scopes = dict.fromkeys(f"范围：{coverage.scope_sources} 篇来源" for coverage in coverages)
        line = f"按提及它的来源篇数统计，{'；'.join(scopes)}。"
    else:
        observed = [_gaps(coverage, query_candidates) for coverage in coverages]
        gaps = "；".join(dict.fromkeys(
            observation[field] for field in _GAP_FIELDS
            for observation in observed if field in observation))
        line = f"按提及它的来源篇数统计，{gaps}，结果可能不全。"
    for count in dict.fromkeys(coverage.duplicate_import_pairs for coverage in coverages
                               if coverage.duplicate_import_pairs):
        line += f"全库有 {count} 对疑似重复导入的来源，计数可能偏高。"
    return line


def _preview_evidence(row, citation):
    return {
        "object_id": row.canonical_id, "object_type": "concept", "name": row.name,
        "definition": "", "snippet": citation.quoted_span, "source_id": citation.source_id,
        "element_id": citation.element_id, "source_title": citation.label,
        "location_label": citation.location_label, "tier": row.tier,
        "notebook_id": row.notebook_id, "relevance": 0.0,
        "provenance": {"producer": "concept_ranking", "authority": "deterministic"},
        "knowhow": None,
    }


def ranking_prompt_block(outcomes, *, keys: CollectionKeyAllocator,
                         budget_chars: int, citations) -> RankingPreview:
    blocks = []
    evidence_by_id = {}
    fingerprints = set()
    rows_left = CONCEPT_RANK_INLINE_ROWS_PER_RUN
    groups = []
    for outcome in _latest_outcomes(outcomes):
        libraries = _inline_libraries(outcome)
        empty_libraries = {
            library.coverage.notebook_id: not library.rows
            for computation in (outcome.computation, *(page.computation for page in outcome.successful_pages))
            for library in computation.libraries
            if library.coverage.status == "ok" and not library.coverage.skipped
        }
        multiple = len({coverage.notebook_id for coverage, _rows in libraries}) > 1
        citable = {}
        for row in (row for _coverage, rows in libraries for row in rows):
            citation = next((citations[key] for index in range(len(row.evidence))
                             if (key := rank_citation_item_id(row.notebook_id, row.canonical_id, index))
                             in citations), None)
            if citation is not None:
                citable[(row.notebook_id, row.canonical_id)] = citation
        for coverage, page_rows in libraries:
            rows = [(row, citable.get((row.notebook_id, row.canonical_id))) for row in page_rows]
            failed = coverage.status != "ok" or bool(coverage.skipped)
            header = (_failure_line("library_skipped" if coverage.skipped else coverage.reason, suffix=False)
                      if failed else _disclosure(coverage, None))
            prefix = (coverage.notebook_name or "另一个笔记本") + "：" if multiple else ""
            groups.append((outcome.fingerprint, coverage, rows, len(page_rows), prefix + header,
                           multiple, _FAILED_INSTRUCTION if failed else
                           (_EMPTY_INSTRUCTION if empty_libraries.get(coverage.notebook_id, False) else "")))

    def render_head(group, shown):
        _fingerprint, _coverage, _rows, total, header, _multiple, instruction = group
        omitted = total - shown
        head = header + (f"另有 {omitted} 行未内联。" if omitted else "")
        return joined([head, *([instruction] if instruction else [])], "\n")

    # Later libraries keep their coverage even when an earlier library has long names.
    tail_reserve = sum(len(render_head(group, 0)) + 2 for group in groups)
    for group in groups:
        fingerprint, coverage, rows, _total, _header, multiple, _incomplete = group
        tail_reserve -= len(render_head(group, 0)) + 2
        for shown in range(min(len(rows), rows_left), -1, -1):
            lines = []
            next_key = keys.next_key
            for row, citation in rows[:shown]:
                marker = f"[k{next_key}] " if citation is not None else ""
                next_key += citation is not None
                line = (f"{marker}{row.name} — "
                        f"在 {row.source_count} 篇来源中出现，共 {row.object_count} 条")
                if row.same_name_group:
                    line += f"（同名组 {row.same_name_group}）"
                if multiple:
                    line += f"（{coverage.notebook_name or '另一个笔记本'}）"
                lines.append(line)
            block = joined([render_head(group, shown),
                            *([entry_lines(lines)] if lines else [])], "\n")
            candidate = joined([_INSTRUCTION, *blocks, block], "\n\n")
            if len(candidate) + tail_reserve > max(0, budget_chars):
                continue
            blocks.append(block)
            for row, citation in rows[:shown]:
                if citation is not None:
                    key = f"k{keys.take(1)}"
                    evidence_by_id[key] = _preview_evidence(row, citation)
            rows_left -= shown
            fingerprints.add(fingerprint)
            break
    return RankingPreview(joined([_INSTRUCTION, *blocks], "\n\n") if blocks else "",
                          evidence_by_id, frozenset(fingerprints))


def _cited_coverages(outcomes, cited_keys, key_owner):
    owners = {key_owner[key] for key in cited_keys if key in key_owner}
    return [coverage for outcome in outcomes for coverage, rows in _inline_libraries(outcome)
            if any((row.notebook_id, row.canonical_id) in owners for row in rows)]


def ranking_notice(outcomes, *, cited_keys: frozenset,
                   key_owner: Mapping[str, tuple[str, str]], query_candidates: int
                   ) -> tuple[str, list[RankingFailure]]:
    outcomes = _latest_outcomes(outcomes)
    has_ranking_reference = bool(cited_keys.intersection(key_owner))
    grouped = {reason: [] for reason in RANK_FAILURE_REASONS}
    all_coverage = [library.coverage for outcome in outcomes for library in outcome.computation.libraries]
    for coverage in all_coverage:
        reason = "library_skipped" if coverage.skipped else coverage.reason
        if reason in grouped and coverage.notebook_id not in grouped[reason]:
            grouped[reason].append(coverage.notebook_id)
    failures = [RankingFailure(
        reason=reason, notebook_ids=ids,
        line=_failure_line(reason, len(ids), suffix=not has_ranking_reference),
        fallback_line=(_failure_line("refresh_produced_nothing", suffix=not has_ranking_reference)
                       if reason == "not_computed" else ""),
    ) for reason, ids in grouped.items() if ids]
    lines = [failure.line for failure in failures]
    multiple = len({coverage.notebook_id for coverage in all_coverage}) > 1
    cited_libraries = {}
    for coverage in _cited_coverages(outcomes, cited_keys, key_owner):
        cited_libraries.setdefault(coverage.notebook_id, []).append(coverage)
    for coverages in cited_libraries.values():
        line = _combined_disclosure(coverages, query_candidates)
        if multiple:
            name = next((coverage.notebook_name for coverage in coverages if coverage.notebook_name),
                        "另一个笔记本")
            line = f"{name}：{line}"
        lines.append(line)
    return "\n".join(lines), failures


def ranking_line_rendered(cited_keys, key_owner, outcomes) -> bool:
    outcomes = _latest_outcomes(outcomes)
    coverages = _cited_coverages(outcomes, cited_keys, key_owner)
    return bool(coverages) and all(coverage.exact for coverage in coverages) and all(
        library.coverage.status == "ok" and not library.coverage.skipped
        for outcome in outcomes for library in outcome.computation.libraries)
