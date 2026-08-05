"""Deterministic corpus disclosure used by deep reports.

This module deliberately works from the report source projection instead of
retrieval hits.  It describes the whole visible notebook collection, while the
retriever remains a relevance-ranked evidence channel.
"""
from __future__ import annotations

from typing import Any, Sequence

from app.services.source_display import source_display_title


def _text(value: Any) -> str:
    return str(value or "").strip()


# Why the profile is missing, when it is.  A scoped run deliberately skips the
# whole-collection aggregate; an aggregation error is a real failure.  Both used
# to persist as a bare ``{}``, so the reader was told "statistics failed" for
# the deliberate case too.  The reason travels with the profile because the
# report body is rendered once at generation time and frozen afterwards.
PROFILE_SCOPE_RESTRICTED = "scope_restricted"
PROFILE_FAILED = "failed"


def unavailable_profile(reason: str) -> dict[str, Any]:
    """Sole constructor for an unavailable profile; carries no statistics."""
    return {"unavailable_reason": reason}


def corpus_profile_available(profile: Any) -> bool:
    """True only for a profile that actually holds aggregated statistics.

    Guards every consumer: an unavailable marker is a non-empty dict, so a bare
    truthiness check would render zeroed counts as if they were measured.
    """
    return bool(profile) and not _text(
        (profile or {}).get("unavailable_reason")
    )


def _normal_title(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def conservative_source_family(row: dict[str, Any]) -> str:
    """Return a conservative family id: exact hash, grounded paper title, id.

    The prefix namespaces the three identifiers.  In particular, an ungrounded
    upload title never merges two documents merely because their file names look
    similar.
    """
    digest = _text(row.get("file_hash"))
    if digest:
        return f"hash:{digest}"
    paper_title = _normal_title(row.get("paper_title"))
    if bool(row.get("is_paper")) and paper_title:
        return f"paper-title:{paper_title}"
    return f"source:{_text(row.get('id'))}"


def conservative_source_families(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Apply both exact-hash and grounded-title rules as a conservative union."""
    parents = list(range(len(rows)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    seen_hash: dict[str, int] = {}
    seen_title: dict[str, int] = {}
    for index, row in enumerate(rows):
        digest = _text(row.get("file_hash"))
        title = (
            _normal_title(row.get("paper_title")) if bool(row.get("is_paper"))
            else ""
        )
        for value, seen in ((digest, seen_hash), (title, seen_title)):
            if not value:
                continue
            if value in seen:
                union(index, seen[value])
            else:
                seen[value] = index

    members: dict[int, list[int]] = {}
    for index in range(len(rows)):
        members.setdefault(root(index), []).append(index)
    keys: dict[str, str] = {}
    for indexes in members.values():
        shared_titles = sorted({
            _normal_title(rows[index].get("paper_title"))
            for index in indexes if bool(rows[index].get("is_paper"))
            and _normal_title(rows[index].get("paper_title"))
        })
        shared_hashes = sorted({
            _text(rows[index].get("file_hash")) for index in indexes
            if _text(rows[index].get("file_hash"))
        })
        if len(indexes) > 1 and len(shared_titles) == 1:
            family = f"paper-title:{shared_titles[0]}"
        elif len(indexes) > 1 and len(shared_hashes) == 1:
            family = f"hash:{shared_hashes[0]}"
        else:
            family = f"source:{min(_text(rows[index].get('id')) for index in indexes)}"
        for index in indexes:
            source_id = _text(rows[index].get("id"))
            if source_id:
                keys[source_id] = family
    return keys


def _representative_row(row: dict[str, Any]) -> dict[str, Any]:
    """Bounded public projection; title selection stays in source_display."""
    return {
        "source_id": row["id"],
        "title": row["display_title"],
        "file_name": _text(row.get("file_name")),
        "doc_type": _text(row.get("doc_type")) or "unknown",
        "pub_year": row.get("pub_year"),
    }


class ReportCorpusProfileService:
    """Build bounded corpus disclosure and resolve only cited source identities."""

    representative_limit = 20

    def __init__(self, source_query: Any):
        self.source_query = source_query

    def build(self, notebook_id: str, *, result_scope: str = "ranked") -> dict[str, Any]:
        raw = self.source_query.report_source_rows(
            notebook_id, representative_limit=self.representative_limit
        )
        snapshot = dict(raw) if isinstance(raw, dict) else {}
        total = int(snapshot.get("total_sources") or 0)
        metadata_sources = int(snapshot.get("metadata_sources") or 0)
        known_year_sources = int(snapshot.get("known_year_sources") or 0)
        identity_uncertain = int(snapshot.get("identity_uncertain_sources") or 0)
        hash_duplicate_excess = int(snapshot.get("hash_duplicate_excess") or 0)
        title_duplicate_excess = int(snapshot.get("title_duplicate_excess") or 0)
        # Hash- and title-duplicate groups may overlap.  Without materialising
        # the full identity graph, max() is a safe "at least" disclosure while
        # their sum would pretend overlapping rows are two duplicates.
        duplicate_lower_bound = max(hash_duplicate_excess, title_duplicate_excess)
        representatives = []
        for raw_row in (snapshot.get("representatives") or [])[: self.representative_limit]:
            row = dict(raw_row)
            row["id"] = _text(row.get("id"))
            row["display_title"] = source_display_title(row)
            if row["display_title"]:
                representatives.append(row)
        scope = _text(result_scope) or "ranked"
        completeness_required = scope in {"complete", "aggregate", "hybrid"}
        return {
            "total_sources": total,
            "identified_duplicate_lower_bound": duplicate_lower_bound,
            "duplicate_identity_overlap_unknown": bool(
                hash_duplicate_excess and title_duplicate_excess
            ),
            "type_distribution": list(snapshot.get("type_distribution") or []),
            "type_distribution_truncated": bool(
                snapshot.get("type_distribution_truncated")
            ),
            "year_distribution": list(snapshot.get("year_distribution") or []),
            "year_distribution_truncated": bool(
                snapshot.get("year_distribution_truncated")
            ),
            "unknown_year": max(0, total - known_year_sources),
            "metadata_sources": metadata_sources,
            "metadata_coverage": (metadata_sources / total if total else 0.0),
            "identity_uncertain_sources": identity_uncertain,
            "identity_uncertain": identity_uncertain > 0,
            "representative_count": len(representatives),
            "representatives": [_representative_row(row) for row in representatives],
            "result_scope": scope,
            "completeness_required": completeness_required,
            # Batch one has no enumeration executor.  This disclosure is data,
            # not prose, so planner and reader views render the same fact.
            "retrieval_mode": "ranked",
            "complete_enumeration_performed": False,
        }

    def resolve_families(self, source_ids: Sequence[str]) -> dict[str, Any]:
        # Callers frequently aggregate ids in a set.  Sorting before the 1,024
        # lookup rail makes the selected window stable across hash seeds and
        # processes instead of resolving an arbitrary subset on each run.
        all_requested = sorted(set(
            str(value) for value in source_ids if str(value or "").strip()
        ))
        requested = all_requested[:1024]
        if not requested:
            return {
                "family_by_source": {}, "uncertain_source_ids": [],
                "unresolved_source_ids": [], "requested_count": 0,
                "truncated": False,
            }
        rows = [
            dict(row) for row in self.source_query.report_source_identity_rows(requested)
        ]
        family_by_source = conservative_source_families(rows)
        row_by_id = {
            _text(row.get("id")): row for row in rows if _text(row.get("id"))
        }
        resolved = set(row_by_id)
        uncertain = [
            source_id for source_id in requested
            if source_id in resolved
            and not _text(row_by_id[source_id].get("file_hash"))
            and not (
                bool(row_by_id[source_id].get("is_paper"))
                and _text(row_by_id[source_id].get("paper_title"))
            )
        ]
        return {
            "family_by_source": family_by_source,
            "uncertain_source_ids": uncertain,
            "unresolved_source_ids": [
                source_id for source_id in requested if source_id not in resolved
            ] + all_requested[1024:],
            "requested_count": len(all_requested),
            "truncated": len(all_requested) > len(requested),
        }


def corpus_profile_planner_block(profile: dict[str, Any]) -> str:
    total = int(profile.get("total_sources") or 0)
    shown = int(profile.get("representative_count") or 0)
    types = "、".join(
        f"{row.get('type') or 'unknown'} {int(row.get('count') or 0)}"
        for row in (profile.get("type_distribution") or [])
    ) or "无"
    if profile.get("type_distribution_truncated"):
        types += "（仅显示主要类型）"
    years = "、".join(
        f"{row.get('year')} {int(row.get('count') or 0)}"
        for row in (profile.get("year_distribution") or [])
    ) or "无已知年份"
    if profile.get("year_distribution_truncated"):
        years += "（仅显示最近年份）"
    lines = [
        f"资料基础:可见来源 {total} 份；完整来源身份未在应用层物化，独立文档族数不作伪精确披露。",
        f"按内容哈希或已校验论文标题，至少识别重复膨胀 {int(profile.get('identified_duplicate_lower_bound') or 0)} 份。",
        f"类型分布:{types}。",
        f"年份分布:{years}；年份未知 {int(profile.get('unknown_year') or 0)} 份。",
        f"论文元数据覆盖 {int(profile.get('metadata_sources') or 0)}/{total}；身份无法可靠合并 {int(profile.get('identity_uncertain_sources') or 0)} 份。",
        f"以下分层代表来源展示 {shown}/共 {total}:",
    ]
    lines.extend(
        f"- {row.get('title') or row.get('file_name') or '(未命名来源)'}"
        + (f" ({row.get('pub_year')})" if row.get("pub_year") else "")
        for row in (profile.get("representatives") or [])
    )
    if profile.get("completeness_required"):
        lines.append("完整性约束:用户要求完整口径，但当前报告仅执行相关性检索，未做完整枚举。")
    return "\n".join(lines)


def corpus_profile_unavailable_copy(profile: dict[str, Any]) -> str:
    """Reader copy for a missing profile, distinguishing intent from failure.

    Legacy reports persisted a bare ``{}`` for both cases and cannot be told
    apart now, so they keep the original failure wording rather than claiming a
    restriction that may never have happened.
    """
    if _text((profile or {}).get("unavailable_reason")) == PROFILE_SCOPE_RESTRICTED:
        return (
            "本次报告限定了检索的资料范围，因此没有统计整个知识库的资料基础；"
            "正文按相关性检索生成，也不能据此推断已覆盖所选资料的全部内容。"
        )
    return "资料基础统计未能完成；正文仍按相关性检索生成，不能据此推断已覆盖全部资料。"


def base_reference_source_count(references: Sequence[Any]) -> int:
    """Distinct reference-library sources actually cited by this report.

    Derived from references that are already assembled, so it costs no query.
    It counts *sources*, not anchors: several anchors into one paper are one
    piece of material, which is the unit the disclosure talks about.
    """
    seen: set[str] = set()
    for reference in references or []:
        row = reference if isinstance(reference, dict) else {}
        if _text(row.get("tier")) != "base":
            continue
        # Base KG evidence can legitimately carry no source_id — the assembler
        # already handles that case, which is what `family_key` is for.  Keying
        # on source_id alone drops those citations and can disclose zero
        # reference-library material while the bibliography clearly shows some.
        key = _text(row.get("source_id")) or _text(row.get("family_key"))
        if key:
            seen.add(key)
    return len(seen)


def corpus_profile_reader_markdown(
    profile: dict[str, Any], *, base_reference_sources: int = 0,
) -> list[str]:
    # The profile counts the current notebook only, while retrieval is federated
    # over mounted reference libraries.  Saying "based on the N visible sources"
    # while most citations came from a library outside that N is the ambiguity
    # this line exists to remove.
    if not corpus_profile_available(profile):
        lines = [
            "## 资料基础",
            "",
            corpus_profile_unavailable_copy(profile),
        ]
        if base_reference_sources > 0:
            # 上面没有任何统计,所以不能说「不计入上述统计」——那会指向一段并
            # 不存在的内容。独立陈述这个事实即可。
            lines.append(
                f"正文引用了 {base_reference_sources} 份来自已挂载参考库的资料。"
            )
        return lines + [""]
    base_note = (
        f"此外，正文还引用了 {base_reference_sources} 份来自已挂载参考库的资料；"
        "参考库资料不计入上述统计。"
        if base_reference_sources > 0 else ""
    )
    total = int(profile.get("total_sources") or 0)
    metadata = int(profile.get("metadata_sources") or 0)
    unknown_year = int(profile.get("unknown_year") or 0)
    lines = [
        "## 资料基础",
        "",
        f"本报告基于当前笔记本可见的 {total} 份资料生成。其中 {metadata}/{total} 份具有已校验的论文元数据，"
        f"{unknown_year} 份年份未知。完整来源身份未载入报告上下文，因此不提供伪精确的独立文档族总数。",
    ]
    if base_note:
        lines.append(base_note)
    duplicate_inflation = int(profile.get("identified_duplicate_lower_bound") or 0)
    if duplicate_inflation:
        lines.append(f"保守规则至少识别出 {duplicate_inflation} 份重复膨胀；哈希与标题重复组可能重叠，未识别重复也仍可能存在。")
    else:
        lines.append("保守信号未识别出重复；这不等于资料中不存在未标注或无法可靠合并的重复。")
    if profile.get("identity_uncertain"):
        lines.append(
            f"有 {int(profile.get('identity_uncertain_sources') or 0)} 份资料缺少可可靠合并的内容哈希或论文标题，独立文档数可能被高估。"
        )
    if profile.get("completeness_required"):
        lines.append("本报告按相关性检索生成，未执行完整枚举；因此不能把正文视为无遗漏清单或精确全库计数。")
    else:
        lines.append("正文证据按与问题的相关性选取，不代表逐份覆盖库内全部资料。")
    return lines + [""]
