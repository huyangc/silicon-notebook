import inspect
import json
from types import SimpleNamespace

from app.core.config import Settings
from app.services.query_intent import confirmed_research_question
from app.services.report_corpus_profile import ReportCorpusProfileService
from app.services.report_engine import (
    audit_high_risk_assertions,
    fair_editor_sections,
)


class _Sources:
    def __init__(self, rows):
        self.rows = rows

    def report_source_rows(self, notebook_id):
        return list(self.rows)


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
    assert profile["independent_families"] == 24
    assert profile["representative_count"] == 20
    assert any(row["doc_type"] == "textbook" for row in profile["representatives"])
    assert profile["unknown_year"] == 1
    assert profile["metadata_sources"] == 24
    assert profile["complete_enumeration_performed"] is False
    assert profile["duplicate_inflation"] == 1


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
    profile = ReportCorpusProfileService(_Sources(rows)).build("nb")
    assert profile["independent_families"] == 1
    assert profile["family_by_source"]["a"] == profile["family_by_source"]["b"]


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
    assert audit["downgraded"] is True


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
        {"markdown": f"## S{index}\n" + (str(index) * 2000) + f"TAIL-{index}"}
        for index in range(4)
    ]
    block = fair_editor_sections(sections, total_chars=4000)
    for index in range(4):
        assert f"## S{index}" in block
        assert f"TAIL-{index}" in block


def test_source_projections_are_uncapped_and_keep_postgres_sqlite_parity():
    from app.repositories.postgres.source_store import SourceStore as PostgresSourceStore
    from app.repositories.sqlite.source_store import SourceStore as SQLiteSourceStore

    for implementation in (PostgresSourceStore, SQLiteSourceStore):
        source = inspect.getsource(implementation.report_source_rows)
        assert "LIMIT 20" not in source
        for field in (
            "s.id", "s.title", "s.file_name", "s.source_type", "s.doc_type",
            "s.file_hash", "m.paper_title", "m.pub_year", "m.is_paper",
        ):
            assert field in source


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
    )
    assert settings.report_high_risk_unsupported_ratio == 0.4


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

    result = _engine(_deps(retrieval=_Retrieval()))._probe_queries(
        "nb", ["q"], family_by_source={
            "s-relevant": "family-relevant",
            "s-noise-a": "family-noise-a",
            "s-noise-b": "family-noise-b",
        },
    )

    assert result["relevant_items"] == 1
    assert result["source_hits"] == 3
    assert result["relevant_family_count"] == 1
