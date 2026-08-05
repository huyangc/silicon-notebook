import inspect
import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.query_intent import confirmed_research_question
from app.services.report_corpus_profile import (
    PROFILE_FAILED,
    PROFILE_SCOPE_RESTRICTED,
    ReportCorpusProfileService,
    base_reference_source_count,
    corpus_profile_available,
    corpus_profile_reader_markdown,
    unavailable_profile,
)
from app.services.report_engine import (
    audit_high_risk_assertions,
)
from app.services.report_synthesis import fair_editor_context


class _Sources:
    def __init__(self, rows):
        self.rows = rows

    def report_source_rows(self, notebook_id, *, representative_limit=20,
                           distribution_limit=32):
        known_years = sum(row.get("pub_year") is not None for row in self.rows)
        metadata = sum(row.get("is_paper") is not None for row in self.rows)
        hash_counts = {}
        title_counts = {}
        for row in self.rows:
            if row.get("file_hash"):
                hash_counts[row["file_hash"]] = hash_counts.get(row["file_hash"], 0) + 1
            if row.get("is_paper") and row.get("paper_title"):
                title = " ".join(row["paper_title"].casefold().split())
                title_counts[title] = title_counts.get(title, 0) + 1
        representatives = []
        seen_types = set()
        for row in self.rows:
            doc_type = row.get("doc_type") or row.get("source_type") or "unknown"
            if doc_type not in seen_types:
                representatives.append(row)
                seen_types.add(doc_type)
        representatives.extend(
            row for row in self.rows if row not in representatives
        )
        return {
            "total_sources": len(self.rows),
            "metadata_sources": metadata,
            "known_year_sources": known_years,
            "identity_uncertain_sources": sum(
                not row.get("file_hash") and not (
                    row.get("is_paper") and row.get("paper_title")
                ) for row in self.rows
            ),
            "hash_duplicate_excess": sum(max(0, count - 1) for count in hash_counts.values()),
            "title_duplicate_excess": sum(max(0, count - 1) for count in title_counts.values()),
            "type_distribution": [],
            "year_distribution": [],
            "representatives": representatives[:representative_limit],
        }

    def report_source_identity_rows(self, source_ids):
        wanted = set(source_ids)
        return [row for row in self.rows if row["id"] in wanted]


def test_corpus_profile_counts_whole_collection_and_stratifies_representatives():
    rows = []
    for index in range(25):
        rows.append({
            "id": f"s-{index:02d}",
            "title": f"Source {index}",
            "file_name": f"{index}.pdf",
            "source_type": "file",
            "doc_type": "academic_paper" if index < 24 else "textbook",
            "file_hash": "same" if index in (0, 1) else f"hash-{index}",
            "is_paper": 1 if index < 24 else None,
            "paper_title": f"Paper {index}" if index < 24 else None,
            "pub_year": 2026 if index < 24 else None,
        })
    profile = ReportCorpusProfileService(_Sources(rows)).build(
        "nb", result_scope="complete"
    )
    assert profile["total_sources"] == 25
    assert profile["representative_count"] == 20
    assert any(row["doc_type"] == "textbook" for row in profile["representatives"])
    assert profile["unknown_year"] == 1
    assert profile["metadata_sources"] == 24
    assert profile["complete_enumeration_performed"] is False
    assert profile["identified_duplicate_lower_bound"] == 1
    assert "family_by_source" not in profile


def test_corpus_profile_merges_equal_grounded_titles_even_when_hashes_differ():
    rows = [{
        "id": "a", "title": "upload a", "file_hash": "hash-a",
        "is_paper": 1, "paper_title": "The Same Paper", "pub_year": 2025,
        "source_type": "file", "doc_type": "academic_paper",
    }, {
        "id": "b", "title": "upload b", "file_hash": "hash-b",
        "is_paper": 1, "paper_title": " The  Same   Paper ", "pub_year": 2025,
        "source_type": "file", "doc_type": "academic_paper",
    }]
    resolution = ReportCorpusProfileService(_Sources(rows)).resolve_families(["a", "b"])
    assert resolution["family_by_source"]["a"] == resolution["family_by_source"]["b"]


def test_family_resolver_caps_identity_hydration_and_discloses_truncation():
    rows = [{
        "id": f"s-{index}", "file_hash": f"h-{index}",
        "is_paper": None, "paper_title": None,
    } for index in range(1026)]
    resolution = ReportCorpusProfileService(_Sources(rows)).resolve_families(
        [row["id"] for row in rows]
    )
    assert resolution["requested_count"] == 1026
    assert resolution["truncated"] is True
    selected = sorted(row["id"] for row in rows)[:1024]
    assert set(resolution["family_by_source"]) == set(selected)
    assert resolution["unresolved_source_ids"] == sorted(
        set(row["id"] for row in rows) - set(selected)
    )


def test_family_resolver_set_input_has_a_deterministic_truncation_window():
    rows = [{
        "id": f"s-{index:04d}", "file_hash": f"h-{index}",
        "is_paper": None, "paper_title": None,
    } for index in range(1030)]
    sources = _Sources(rows)
    forward = ReportCorpusProfileService(sources).resolve_families(
        {row["id"] for row in rows}
    )
    reverse = ReportCorpusProfileService(sources).resolve_families(
        {row["id"] for row in reversed(rows)}
    )
    assert forward == reverse
    assert list(forward["family_by_source"]) == sorted(row["id"] for row in rows)[:1024]


def test_scoped_skip_and_aggregation_failure_read_as_different_things():
    """A scoped run skipped the aggregate on purpose; nothing broke."""
    scoped = corpus_profile_reader_markdown(
        unavailable_profile(PROFILE_SCOPE_RESTRICTED)
    )
    failed = corpus_profile_reader_markdown(unavailable_profile(PROFILE_FAILED))

    assert "## 资料基础" in scoped and "## 资料基础" in failed
    scoped_copy, failed_copy = "\n".join(scoped), "\n".join(failed)
    assert "限定了检索的资料范围" in scoped_copy
    assert "未能完成" not in scoped_copy
    assert "资料基础统计未能完成" in failed_copy
    # Legacy reports stored a bare `{}` and genuinely cannot be told apart, so
    # they must not be relabelled as scoped after the fact.
    legacy_copy = "\n".join(corpus_profile_reader_markdown({}))
    assert "资料基础统计未能完成" in legacy_copy
    assert "限定了检索的资料范围" not in legacy_copy


def test_reader_disclosure_names_reference_library_material_it_cited():
    """The profile counts one notebook; retrieval is federated over mounted bases.

    Real data from a production report: profile total 4, yet 26 of 42 anchors
    resolved to a mounted base.  "Based on the 4 visible sources" then reads as
    the whole evidence basis, which is the ambiguity being removed here.
    """
    references = [
        {"key": "k1", "tier": "base", "source_id": "src-b1"},
        {"key": "k2", "tier": "base", "source_id": "src-b1"},   # 同一份资料的第二个锚点
        {"key": "k3", "tier": "base", "source_id": "src-b2"},
        {"key": "k4", "tier": "personal", "source_id": "src-p1"},
        {"key": "k5", "source_id": "src-p2"},                   # 无 tier 视为本地
    ]
    assert base_reference_source_count(references) == 2

    profile = {"total_sources": 4, "metadata_sources": 4, "unknown_year": 2}
    markdown = "\n".join(
        corpus_profile_reader_markdown(profile, base_reference_sources=2)
    )
    assert "当前笔记本可见的 4 份资料" in markdown
    assert "引用了 2 份来自已挂载参考库的资料" in markdown

    # No base citations → no extra sentence, so single-library reports read
    # exactly as before.
    local_only = "\n".join(corpus_profile_reader_markdown(profile))
    assert "参考库" not in local_only

    # The note must survive an unavailable profile too: a scoped run that cited
    # a base library is precisely where the reader is most likely to miscount.
    scoped = "\n".join(corpus_profile_reader_markdown(
        unavailable_profile(PROFILE_SCOPE_RESTRICTED), base_reference_sources=3,
    ))
    assert "限定了检索的资料范围" in scoped and "3 份来自已挂载参考库" in scoped
    # 没有统计可言时不能说「不计入上述统计」——那会指向一段并不存在的内容。
    assert "不计入上述统计" not in scoped
    legacy = "\n".join(corpus_profile_reader_markdown({}, base_reference_sources=1))
    assert "1 份来自已挂载参考库" in legacy and "不计入上述统计" not in legacy


def test_unavailable_profile_is_never_treated_as_measured_statistics():
    """An unavailable marker is a non-empty dict; truthiness is not enough."""
    assert corpus_profile_available({}) is False
    assert corpus_profile_available(None) is False
    assert corpus_profile_available(unavailable_profile(PROFILE_FAILED)) is False
    assert corpus_profile_available(
        unavailable_profile(PROFILE_SCOPE_RESTRICTED)
    ) is False
    assert corpus_profile_available({"total_sources": 3}) is True


def test_high_risk_audit_requires_valid_same_sentence_anchor_and_exempts_marked_prose():
    markdown = """## 结果
吞吐提升 25% [k1]。复杂度为 O(L^2)。
（推断）可能快 3 倍。
【通识】典型容量为 8GB。
排名第一 [k404]。
"""
    audit = audit_high_risk_assertions(
        markdown, {"k1": {"object_id": "x"}}, max_unsupported_ratio=0.25
    )
    assert audit["high_risk_assertions"] == 3
    assert audit["supported"] == 1
    assert audit["unsupported"] == 2
    assert audit["threshold_exceeded"] is True
    assert audit["downgrade_applied"] is False


def test_high_risk_audit_ignores_ordinary_prose_and_section_numbers():
    markdown = """## 普通说明
平均而言，这是一种 parallel implementation challenge。
Nevertheless, alluvial layers are described in Chapter 2.
第 2 节介绍流程，图 3 展示结构。
第一章介绍背景，第一步执行检索。
"""
    audit = audit_high_risk_assertions(markdown, {}, max_unsupported_ratio=0.25)
    assert audit["high_risk_assertions"] == 0
    assert audit["threshold_exceeded"] is False


def test_high_risk_audit_detects_unit_numbers_joined_to_chinese_prose():
    markdown = """## 中文数字断言
吞吐降低了30%。容量提升2倍[k1]。资料共收录128篇。
"""
    audit = audit_high_risk_assertions(
        markdown, {"k1": {"object_id": "x"}}, max_unsupported_ratio=0.25
    )
    assert audit["high_risk_assertions"] == 3
    assert audit["supported"] == 1
    assert audit["unsupported"] == 2
    assert audit["unsupported_samples"] == ["吞吐降低了30%。", "资料共收录128篇。"]


def test_high_risk_audit_ignores_ordinals_and_operation_names():
    """序数与操作名不是数量断言(#425 复核 P2):「第3层」指称一个层的位置、
    「all-reduce」是通信原语的名字,都不该进高风险分母——那个分母直接决定
    REPORT_HIGH_RISK_DOWNGRADE_ENABLED 默认值要用的真机分布,尺子必须先准。
    无空格(第3层)与单空格(第 1 个)两种排版都要挡;真正的数量断言
    (12层/30%)在同一段里必须照常命中,防止把「排除序数」写宽成「排除一切
    带层/个的数字」。"""
    markdown = """## 序数与操作名
第3层使用了残差连接。第 1 个方案已被否决。这是第2篇论文。
all-reduce 操作在此执行。all-to-all 通信随后进行。
该模型共有12层[k1]。端到端延迟降低了30%。
"""
    audit = audit_high_risk_assertions(
        markdown, {"k1": {"object_id": "x"}}, max_unsupported_ratio=0.25
    )
    # 只有「12层」「30%」两句是真断言;序数三句与 all-reduce 一句都不计。
    assert audit["high_risk_assertions"] == 2
    assert audit["supported"] == 1
    assert audit["unsupported_samples"] == ["端到端延迟降低了30%。"]


def test_report_retrieval_query_can_exclude_assumptions_without_losing_constraints():
    contract = {
        "resolved_question": "比较 A 与 B",
        "constraints": ["相同训练预算"],
        "assumptions": ["A 一定优于 B"],
    }
    query = confirmed_research_question(
        contract, "fallback", include_assumptions=False
    )
    assert "相同训练预算" in query
    assert "A 一定优于 B" not in query
    assert "A 一定优于 B" in confirmed_research_question(contract, "fallback")


def test_fair_editor_context_includes_every_section_tail():
    sections = [
        {
            "title": f"S{index}",
            "markdown": f"## S{index}\n" + (str(index) * 3000) + f"TAIL-{index}",
            "claims": [
                {"claim_id": f"c-{index}-{claim}", "statement": "x" * 500}
                for claim in range(24)
            ],
            "claim_ledger_status": "available",
        }
        for index in range(6)
    ]
    block = fair_editor_context(
        sections,
        frame={"subject_kind": "model", "facets": [], "axes": []},
        blueprint={"central_answer": "z" * 20_000, "claims": [{}] * 96},
        max_chars=24_000,
    )
    assert len(block) <= 24_000
    assert "blueprint_summary" in block
    for index in range(6):
        assert f'"title": "S{index}"' in block
        assert f"TAIL-{index}" in block
    assert '"claims_truncated": true' in block


def test_fair_editor_context_bounds_maximum_contract_without_broken_json():
    frame = {
        "subject_kind": "x" * 160,
        "facets": [
            {
                "id": f"facet-{facet}",
                "name": f"Facet {facet}",
                "values": [f"value-{value}" for value in range(12)],
                "exclusive": True,
            }
            for facet in range(8)
        ],
        "axes": [
            {"id": f"axis-{axis}", "name": f"Axis {axis}", "condition_fields": ["x"] * 8}
            for axis in range(8)
        ],
    }
    sections = []
    for section_index in range(6):
        claims = []
        for claim_index in range(24):
            claims.append({
                "claim_id": f"claim-{section_index}-{claim_index}",
                "statement": f"claim {claim_index} " + ("x" * 1580),
                "type": "comparison",
                "entities": [f"entity-{claim_index}"],
                "evidence_keys": [f"k{key}" for key in range(16)],
                "conditions": [("condition-" + ("y" * 290))] * 8,
                "confidence": 0.9,
                "frame_assignments": {
                    f"facet-{facet}": f"value-{section_index % 2}"
                    for facet in range(8)
                },
            })
        sections.append({
            "title": f"MAX-SECTION-{section_index}",
            "markdown": f"HEAD-{section_index}\n" + ("body" * 2000) + f"TAIL-{section_index}",
            "claims": claims,
            "claim_ledger_status": "available",
            "citation_audit": {
                "high_risk_assertions": 100,
                "supported": 60,
                "unsupported": 40,
                "unsupported_ratio": 0.4,
                "threshold": 0.25,
                "threshold_exceeded": True,
                "unsupported_samples": [("unsupported " + ("z" * 1000))] * 100,
                "unbounded_internal_field": "must-not-cross-editor-boundary" * 1000,
            },
        })
    block = fair_editor_context(
        sections,
        frame=frame,
        blueprint={
            "central_answer": "z" * 20_000,
            "shared_definitions": [{}] * 24,
            "claims": [{"statement": "q" * 1600}] * 96,
            "sections": [{}] * 6,
        },
        max_chars=24_000,
    )

    assert len(block) <= 24_000
    head, *section_blocks = block.split("\n\n")
    overhead = json.loads(head.removeprefix("Report audit contracts:\n"))
    assert overhead["blueprint_summary"]["status"] == "summarized"
    assert overhead["conflicts_total"] >= len(overhead["conflicts"])
    assert all(len(value) <= 360 for value in overhead["conflicts"])
    assert len(section_blocks) == 6
    for section_index, section_block in enumerate(section_blocks):
        meta_text, prose = section_block.split("\nProse excerpt:\n", 1)
        meta = json.loads(meta_text)
        assert meta["title"] == f"MAX-SECTION-{section_index}"
        assert meta["claims_truncated"] is True
        assert meta["citation_audit"]["unsupported_samples_total"] == 100
        assert "unbounded_internal_field" not in meta["citation_audit"]
        assert f"HEAD-{section_index}" in prose
        assert f"TAIL-{section_index}" in prose


def test_source_projections_are_bounded_aggregates_with_identity_lookup_parity():
    from app.repositories.postgres.source_store import SourceStore as PostgresSourceStore
    from app.repositories.sqlite.source_store import SourceStore as SQLiteSourceStore

    for implementation in (PostgresSourceStore, SQLiteSourceStore):
        source = inspect.getsource(implementation.report_source_rows)
        assert "COUNT(*) AS total_sources" in source
        assert "LIMIT" in source
        assert "representatives" in source
        identity = inspect.getsource(implementation.report_source_identity_rows)
        assert "[:1024]" in identity
        assert "file_hash" in identity and "paper_title" in identity


def test_report_source_facade_methods_are_one_hop_delegates():
    from app.services.repository_facade import RepositoryFacade

    for method_name in ("report_source_rows", "report_source_identity_rows"):
        source = inspect.getsource(getattr(RepositoryFacade, method_name))
        assert "self._runtime.source_store" in source


def test_shared_report_intent_preserves_complete_scope():
    from tests.test_report_engine_ports import _deps, _engine

    class _IntentLLM:
        configured = True
        model = "intent"

        def chat_json(self, messages, schema_hint, **kwargs):
            assert '"result_scope"' in schema_hint
            return json.dumps({
                "normalized_question": "列出全部方法",
                "result_scope": "complete",
                "completeness_required": True,
                "mandatory_topics": [{
                    "title": "全部方法", "question": "列出全部方法",
                    "retrieval_queries": ["all methods"],
                }],
            })

    deps = _deps()
    object.__setattr__(deps, "model_clients", type("Models", (), {
        "chat": lambda self, workload: _IntentLLM(),
        "parallelism": lambda self, workload: 1,
    })())
    contract = _engine(deps)._plan_intent_contract("列出全部方法", "")
    assert contract["result_scope"] == "complete"
    assert contract["completeness_required"] is True


def test_high_risk_threshold_is_configurable():
    settings = Settings(
        _env_file=None,
        REPORT_HIGH_RISK_UNSUPPORTED_RATIO="0.4",
        REPORT_HIGH_RISK_DOWNGRADE_ENABLED="true",
    )
    assert settings.report_high_risk_unsupported_ratio == 0.4
    assert settings.report_high_risk_downgrade_enabled is True
    assert Settings(_env_file=None).report_high_risk_downgrade_enabled is False


def test_sufficiency_probe_excludes_low_relevance_sources_from_family_counts():
    from tests.test_report_engine_ports import _deps, _engine

    class _Retrieval:
        def federated_retrieve(self, notebook_id, query):
            return [
                SimpleNamespace(
                    object_id="relevant", relevance=0.9, tier="active",
                    evidence=[SimpleNamespace(source_id="s-relevant")],
                ),
                SimpleNamespace(
                    object_id="noise-a", relevance=0.01, tier="active",
                    evidence=[SimpleNamespace(source_id="s-noise-a")],
                ),
                SimpleNamespace(
                    object_id="noise-b", relevance=0.01, tier="active",
                    evidence=[SimpleNamespace(source_id="s-noise-b")],
                ),
            ]

        def retrieve_elements(self, notebook_id, query, *, limit):
            return []

    sources = _Sources([
        {"id": source_id, "file_hash": source_id, "is_paper": None,
         "paper_title": None}
        for source_id in ("s-relevant", "s-noise-a", "s-noise-b")
    ])
    deps = _deps(retrieval=_Retrieval(), source_query=sources)
    result = _engine(deps)._probe_queries("nb", ["q"])

    assert result["relevant_items"] == 1
    assert result["source_hits"] == 3
    assert result["relevant_family_count"] == 1


def test_sufficiency_judge_cannot_promote_one_family_even_with_many_hits():
    from tests.test_report_engine_ports import _deps, _engine

    class _Judge:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "verdicts": [{"title": "A", "sufficiency": "充足"}]
            })

    deps = _deps()
    object.__setattr__(deps, "model_clients", type("Models", (), {
        "chat": lambda self, workload: _Judge(),
        "parallelism": lambda self, workload: 1,
    })())
    sections = _engine(deps)._judge_sufficiency(
        "q", [{"title": "A"}], [{
            "title": "A", "hits": 1000, "base_hits": 1000,
            "element_hits": 96, "source_hits": 1,
            "relevant_items": 1000, "relevant_supports": 1000,
            "relevant_family_count": 1, "top_family_share": 1.0,
            "source_identity_uncertain": 0,
        }],
    )
    assert sections[0]["sufficiency"] == "薄弱"


def test_probe_deduplicates_same_evidence_across_queries_and_keeps_share_units_aligned():
    from tests.test_report_engine_ports import _deps, _engine

    class _Retrieval:
        def federated_retrieve(self, notebook_id, query):
            return [SimpleNamespace(
                object_id="same", relevance=0.9, tier="active",
                evidence=[SimpleNamespace(source_id="s1")],
            )]

        def retrieve_elements(self, notebook_id, query, *, limit):
            return []

    sources = _Sources([{
        "id": "s1", "file_hash": "h1", "is_paper": None, "paper_title": None,
    }])
    result = _engine(_deps(retrieval=_Retrieval(), source_query=sources))._probe_queries(
        "nb", ["q1", "q2"]
    )
    assert result["relevant_items"] == 1
    assert result["relevant_supports"] == 1
    assert result["top_family_share"] == 1.0


def test_probe_does_not_count_unidentified_sources_as_independent_families():
    from tests.test_report_engine_ports import _deps, _engine

    class _Retrieval:
        def federated_retrieve(self, notebook_id, query):
            return [SimpleNamespace(
                object_id="claim", relevance=0.9, tier="active",
                evidence=[SimpleNamespace(source_id="s-unknown")],
            )]

        def retrieve_elements(self, notebook_id, query, *, limit):
            return []

    sources = _Sources([{
        "id": "s-unknown", "file_hash": "", "is_paper": None,
        "paper_title": None,
    }])
    result = _engine(_deps(retrieval=_Retrieval(), source_query=sources))._probe_queries(
        "nb", ["q"]
    )
    assert result["source_identity_uncertain"] == 1
    assert result["relevant_family_count"] == 0


def test_probe_uses_unknown_identity_as_a_conservative_top1_upper_bound():
    """Seven A / one B / six unknown supports must not look 50% diverse.

    The unknown identities are excluded from the independent-family count, but
    any of them could be another copy of A.  The coverage signal must therefore
    expose 13/14, not split them into a friendly synthetic family bucket.
    """
    from tests.test_report_engine_ports import _deps, _engine

    class _Retrieval:
        def federated_retrieve(self, notebook_id, query):
            return [
                SimpleNamespace(
                    object_id=f"a-{index}", relevance=0.9, tier="active",
                    evidence=[SimpleNamespace(source_id="source-a")],
                )
                for index in range(7)
            ] + [
                SimpleNamespace(
                    object_id="b-0", relevance=0.9, tier="active",
                    evidence=[SimpleNamespace(source_id="source-b")],
                )
            ] + [
                SimpleNamespace(
                    object_id=f"unknown-{index}", relevance=0.9, tier="active",
                    evidence=[SimpleNamespace(source_id=f"source-u-{index}")],
                )
                for index in range(6)
            ]

        def retrieve_elements(self, notebook_id, query, *, limit):
            return []

    sources = _Sources([
        {"id": "source-a", "file_hash": "hash-a", "is_paper": None,
         "paper_title": None},
        {"id": "source-b", "file_hash": "hash-b", "is_paper": None,
         "paper_title": None},
        *[
            {"id": f"source-u-{index}", "file_hash": "", "is_paper": None,
             "paper_title": None}
            for index in range(6)
        ],
    ])
    result = _engine(_deps(
        retrieval=_Retrieval(), source_query=sources
    ))._probe_queries("nb", ["q"])

    assert result["relevant_supports"] == 14
    assert result["relevant_family_count"] == 2
    assert result["unknown_supports"] == 6
    assert result["source_identity_uncertain"] == 6
    assert result["top_family_share"] == pytest.approx(13 / 14)
