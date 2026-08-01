"""Deterministic corpus disclosure used by deep reports.

This module deliberately works from the report source projection instead of
retrieval hits.  It describes the whole visible notebook collection, while the
retriever remains a relevance-ranked evidence channel.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from app.services.source_display import source_display_title


def _text(value: Any) -> str:
    return str(value or "").strip()


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
        "family_key": row["family_key"],
    }


class ReportCorpusProfileService:
    """Build a stable, bounded presentation over all visible source rows."""

    representative_limit = 20

    def __init__(self, source_query: Any):
        self.source_query = source_query

    def build(self, notebook_id: str, *, result_scope: str = "ranked") -> dict[str, Any]:
        rows = [dict(row) for row in self.source_query.report_source_rows(notebook_id)]
        family_keys = conservative_source_families(rows)
        enriched: list[dict[str, Any]] = []
        for position, row in enumerate(rows):
            row["id"] = _text(row.get("id"))
            row["display_title"] = source_display_title(row)
            row["family_key"] = family_keys.get(
                row["id"], conservative_source_family(row)
            )
            row["_position"] = position
            enriched.append(row)

        type_counts = Counter(
            _text(row.get("doc_type")) or _text(row.get("source_type")) or "unknown"
            for row in enriched
        )
        known_years: list[int] = []
        for row in enriched:
            try:
                year = int(row.get("pub_year"))
            except (TypeError, ValueError):
                continue
            if 1000 <= year <= 9999:
                row["pub_year"] = year
                known_years.append(year)
        year_counts = Counter(known_years)
        metadata_sources = sum(
            1 for row in enriched
            if row.get("is_paper") is not None
        )

        # Stable stratification: first source of every type, then every known
        # year, then creation order.  It avoids an early-uploaded topical run
        # monopolising the planner's bounded representative list.
        representatives: list[dict[str, Any]] = []
        selected: set[str] = set()

        def take(candidates: Iterable[dict[str, Any]]) -> None:
            for row in candidates:
                source_id = row["id"]
                if not source_id or source_id in selected:
                    continue
                representatives.append(row)
                selected.add(source_id)
                break

        for name in sorted(type_counts):
            take(row for row in enriched if (
                _text(row.get("doc_type")) or _text(row.get("source_type")) or "unknown"
            ) == name)
        for year in sorted(year_counts, reverse=True):
            take(row for row in enriched if row.get("pub_year") == year)
        for row in enriched:
            if len(representatives) >= self.representative_limit:
                break
            if row["id"] not in selected:
                representatives.append(row)
                selected.add(row["id"])

        total = len(enriched)
        families = {row["family_key"] for row in enriched}
        unknown_year = total - sum(year_counts.values())
        identity_uncertain = sum(
            1 for row in enriched
            if not _text(row.get("file_hash"))
            and not (bool(row.get("is_paper")) and _text(row.get("paper_title")))
        )
        scope = _text(result_scope) or "ranked"
        completeness_required = scope in {"complete", "aggregate", "hybrid"}
        return {
            "total_sources": total,
            "independent_families": len(families),
            "duplicate_inflation": max(0, total - len(families)),
            "family_by_source": {
                row["id"]: row["family_key"] for row in enriched if row["id"]
            },
            "type_distribution": [
                {"type": name, "count": count}
                for name, count in sorted(type_counts.items())
            ],
            "year_distribution": [
                {"year": year, "count": count}
                for year, count in sorted(year_counts.items(), reverse=True)
            ],
            "unknown_year": unknown_year,
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


def corpus_profile_planner_block(profile: dict[str, Any]) -> str:
    total = int(profile.get("total_sources") or 0)
    shown = int(profile.get("representative_count") or 0)
    types = "、".join(
        f"{row.get('type') or 'unknown'} {int(row.get('count') or 0)}"
        for row in (profile.get("type_distribution") or [])
    ) or "无"
    years = "、".join(
        f"{row.get('year')} {int(row.get('count') or 0)}"
        for row in (profile.get("year_distribution") or [])
    ) or "无已知年份"
    lines = [
        f"资料基础:可见来源 {total} 份；保守去重后 {int(profile.get('independent_families') or 0)} 个文档族。",
        f"保守规则识别的重复膨胀 {int(profile.get('duplicate_inflation') or 0)} 份。",
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


def corpus_profile_reader_markdown(profile: dict[str, Any]) -> list[str]:
    if not profile:
        return [
            "## 资料基础",
            "",
            "资料基础统计未能完成；正文仍按相关性检索生成，不能据此推断已覆盖全部资料。",
            "",
        ]
    total = int(profile.get("total_sources") or 0)
    families = int(profile.get("independent_families") or 0)
    metadata = int(profile.get("metadata_sources") or 0)
    unknown_year = int(profile.get("unknown_year") or 0)
    lines = [
        "## 资料基础",
        "",
        f"本报告基于当前可见的 {total} 份资料生成；按保守规则可区分为 {families} 个文档族。"
        f"其中 {metadata}/{total} 份具有已校验的论文元数据，{unknown_year} 份年份未知。",
    ]
    duplicate_inflation = int(profile.get("duplicate_inflation") or 0)
    if duplicate_inflation:
        lines.append(f"保守规则识别出 {duplicate_inflation} 份重复膨胀；未被可靠识别的重复仍可能存在。")
    if profile.get("identity_uncertain"):
        lines.append(
            f"有 {int(profile.get('identity_uncertain_sources') or 0)} 份资料缺少可可靠合并的内容哈希或论文标题，独立文档数可能被高估。"
        )
    if profile.get("completeness_required"):
        lines.append("本报告按相关性检索生成，未执行完整枚举；因此不能把正文视为无遗漏清单或精确全库计数。")
    else:
        lines.append("正文证据按与问题的相关性选取，不代表逐份覆盖库内全部资料。")
    return lines + [""]
