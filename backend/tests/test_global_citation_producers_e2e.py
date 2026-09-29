"""PR-D closing round -- healthy-run proofs through the REAL global engine.

Each producer that can mint a card or an anchor in a global run is driven end
to end through ``GlobalAskService`` (``global_ask_run``, the job thread, the
shared engine, the terminal check) over a real SQLite repository. A healthy
run -- nothing edited, nothing deleted -- must deliver with no ``verification``
anywhere and no ``citation_check``, and the producer's registration must be
visible as a ``producer_evidence_attested`` event. The mix branch's KG-overlay
leg also gets its race cases here (UPDATE -> changed, DELETE -> source_gone);
the PostgreSQL half of every race is in ``tests/postgres``.

These runs are what found the peer-mode attribution gaps fixed in this round
(overlay passages and rescored KG hits without a library), which the
hand-assembled producer harnesses could not see.
"""
from __future__ import annotations

import pytest

from tests import global_citation_e2e_kit as kit
from tests.model_testkit import bind_rerank_client


# ---------------------------------------------------------------------------
# One healthy run per producer
# ---------------------------------------------------------------------------

def test_collection_enumeration_of_elements_is_clean(tmp_path, monkeypatch):
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s-f", "公式集", ["公式 一 E=mc2", "公式 二 F=ma"],
                          element_type="formula")
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent([{
            "next_action": "enumerate_elements", "enumerate": {"kind": "formula"},
            "reason": "列出公式",
        }]))
        result = kit.global_answer(repo, [nb.id], "列出所有公式")
        kit.assert_clean(result, events, "collection_enumeration")
        assert {r.element_id for r in kit.references(result.answer)} >= {"s-f-000", "s-f-001"}
    finally:
        repo.close()


def test_collection_enumeration_of_kg_objects_is_clean(tmp_path, monkeypatch):
    """Enumerated KG rows (collection producer) plus the KG cards and anchors
    of the retrieved objects (``kg_objects`` producer), in one run. This is
    the run that exposed the closing rerank dropping the library of a KG hit."""
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s-a", "论文", ["低温下增益上升的实验记录", "常温基线"])
        repo.store_kg(nb.id, "s-a", [
            {"local_id": "C1", "object_type": "claim",
             "payload": {"name": "低温增益上升", "section_path": "1"},
             "evidence": [kit.evidence("s-a", "s-a-000", "低温下增益")]},
            {"local_id": "C2", "object_type": "claim",
             "payload": {"name": "常温基线稳定", "section_path": "2"},
             "evidence": [kit.evidence("s-a", "s-a-001", "常温基线")]},
        ], [])
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent([{
            "next_action": "enumerate_kg_objects", "enumerate": {"object_type": "claim"},
            "reason": "列出论断",
        }]))
        result = kit.global_answer(repo, [nb.id], "列出所有论断")
        kit.assert_clean(result, events, "collection_enumeration")
        kit.assert_clean(result, events, "kg_objects")
        assert all(r.notebook_id == nb.id for r in kit.references(result.answer))
    finally:
        repo.close()


def test_document_read_is_clean(tmp_path, monkeypatch):
    from tests.test_reasoning_document_read import _read_action
    from tests.test_reasoning_enumeration_tools import _enumerate_sources_action

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s-empty", "无摘要文档",
                          ["起点:版图取样方法。", "中段:实验。", "末尾结论:更好。"])
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent([
            _enumerate_sources_action(), _read_action("无摘要文档"),
        ]))
        result = kit.global_answer(repo, [nb.id], "这个库的文档分别讲了什么")
        kit.assert_clean(result, events, "document_overview")
        assert {"s-empty-000", "s-empty-002"} <= {
            r.element_id for r in kit.references(result.answer)
        }
    finally:
        repo.close()


def test_follow_chain_is_clean(tmp_path, monkeypatch):
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s-c", "推导", ["A 推出 B 的原文", "B 推出 C 的原文"])
        repo.store_kg(nb.id, "s-c", [
            {"local_id": "A", "object_type": "claim",
             "payload": {"name": "前提甲", "section_path": "A"},
             "evidence": [kit.evidence("s-c", "s-c-000", "A 推出 B")]},
            {"local_id": "B", "object_type": "claim",
             "payload": {"name": "桥梁乙", "section_path": "B"},
             "evidence": [kit.evidence("s-c", "s-c-001", "B 推出 C")]},
            {"local_id": "C", "object_type": "claim",
             "payload": {"name": "结论丙", "section_path": "C"},
             "evidence": [kit.evidence("s-c", "s-c-001", "B 推出 C")]},
        ], [
            {"source_local_id": "A", "target_local_id": "B", "edge_type": "derived_from",
             "evidence": [kit.evidence("s-c", "s-c-000", "A 推出 B")]},
            {"source_local_id": "B", "target_local_id": "C", "edge_type": "derived_from",
             "evidence": [kit.evidence("s-c", "s-c-001", "B 推出 C")]},
        ])
        start = kit.object_ids(repo, nb.id)["前提甲"]
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent([{
            "next_action": "follow_chain",
            "follow_chain": {"start_object_id": start, "edge_type": "derived_from",
                             "direction": "out"},
            "reason": "推导",
        }], plan_query="前提甲"))
        result = kit.global_answer(repo, [nb.id], "前提甲能推出什么")
        kit.assert_clean(result, events, "follow_chain")
        assert [a.object_type for a in result.answer.anchors].count("relation") == 2
    finally:
        repo.close()


def test_table_analysis_is_clean(tmp_path, monkeypatch):
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.upload_workbook(repo, nb.id, tmp_path, [("East", 10), ("West", 20)])
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent())
        result = kit.global_answer(repo, [nb.id], "分析 sales.xlsx 工作表的数据质量和缺失情况")
        kit.assert_clean(result, events, "table_analysis")
        assert any(a.key == "k6001" for a in result.answer.anchors)
    finally:
        repo.close()


def test_a_table_filter_matching_nothing_passes_at_source_level(tmp_path, monkeypatch):
    """Closing item 17: zero result rows -> no row citation. The receipt's
    anchor now takes the library from the RESULT, so it passes at source level
    instead of being judged unattributed."""
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        source_id = kit.upload_workbook(repo, nb.id, tmp_path, [("East", 10), ("West", 20)])
        events = kit.capture_events(repo, monkeypatch)
        kit.bind_models(repo, kit.ScriptedAgent(sheet_plan={
            "source_id": source_id, "sheet": "Sales", "operation": "filter",
            "filters": [{"column": "Region", "operator": "eq", "value": "North"}],
            "columns": ["Region", "Amount"],
        }))
        result = kit.global_answer(repo, [nb.id], "筛选 sales.xlsx 工作表里 Region 为 North 的行")
        assert result.status == "done", result.error
        [receipt] = [a for a in result.answer.anchors if a.key == "k6001"]
        assert (receipt.notebook_id, receipt.source_id, receipt.element_id) == (nb.id, source_id, "")
        assert receipt.verification is None
        assert result.answer.citation_check is None
        [table] = [r for r in result.answer.result_sets if r.kind == "spreadsheet"]
        assert table.rows == [] and table.notebook_id == nb.id
        assert not [e for e in events if e.get("kind") == "global_ask_citation_scope_diagnostic"]
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Mix branch, KG-overlay leg (closing item 16)
# ---------------------------------------------------------------------------

class _Rerank:
    configured = True

    def rerank(self, query, documents, on_error=None):
        return list(range(len(documents)))


def _overlay_only_mix(tmp_path, monkeypatch):
    """A global chunk-mode run on the mix branch whose pool holds ONLY
    overlay passages: the vector leg is emptied, PPR is off in peer mode."""
    from app.services.retrieval_candidates import CandidateRetrievalService
    from tests.test_global_ask_engine_parity import _e2e_repo

    repo, notebooks, user_id = _e2e_repo(tmp_path, monkeypatch, answer="低温性能如下。")
    repo.settings.chunk_kg_overlay_enabled = True
    bind_rerank_client(repo, _Rerank())
    nb = notebooks[0]
    with repo._runtime.source_store.database.connect() as db:
        row = db.execute(
            "SELECT e.id, e.source_id FROM source_elements e JOIN sources s "
            "ON s.id=e.source_id WHERE s.notebook_id=? AND e.text LIKE '%零下四十度%'",
            (nb.id,),
        ).fetchone()
    repo.store_kg(nb.id, row["source_id"], [{
        "local_id": "a", "object_type": "concept",
        "payload": {"name": "低温性能", "definition": "低温下增益上升"},
        "evidence": [kit.evidence(row["source_id"], row["id"], "零下四十度")],
    }], [])
    overlay: list = []
    original = CandidateRetrievalService._kg_source_chunks

    def spy(self, *args, **kwargs):
        chunks = original(self, *args, **kwargs)
        overlay.extend(chunk.chunk_id for chunk in chunks)
        return chunks

    monkeypatch.setattr(CandidateRetrievalService, "_kg_source_chunks", spy)
    monkeypatch.setattr(CandidateRetrievalService, "_gather_vector_chunks",
                        lambda self, notebook_id, sub_queries: [])
    return repo, notebooks, user_id, row["id"], overlay


def _chunk_answer(repo, notebooks, user_id):
    from tests.test_global_ask_engine_parity import _e2e_answer

    return _e2e_answer(repo, notebooks, user_id, "低温性能如何")


def test_an_overlay_only_passage_is_registered_and_the_run_is_clean(tmp_path, monkeypatch):
    repo, notebooks, user_id, element_id, overlay = _overlay_only_mix(tmp_path, monkeypatch)
    try:
        events = kit.capture_events(repo, monkeypatch)
        result = _chunk_answer(repo, notebooks, user_id)
        assert overlay, "the run never took the mix branch's overlay leg"
        kit.assert_clean(result, events, "kg_overlay_passages")
        cited = [r for r in kit.references(result.answer) if r.element_id == element_id]
        assert cited and all(r.notebook_id == notebooks[0].id for r in cited)
    finally:
        repo.close()


@pytest.mark.parametrize("mutation,expected", [("update", "changed"), ("delete", "source_gone")])
def test_an_overlay_only_passage_race_before_the_terminal_read(
    tmp_path, monkeypatch, mutation, expected,
):
    from app.services import global_citation_check as check_module

    repo, notebooks, user_id, element_id, overlay = _overlay_only_mix(tmp_path, monkeypatch)
    try:
        database = repo._runtime.source_store.database
        original = check_module.GlobalCitationCheck._read_current

        def mutate_then_read(self, references, siblings, event):
            with database.write() as db:
                if mutation == "update":
                    db.execute("UPDATE source_elements SET text=text||'（改）' WHERE id=?",
                               (element_id,))
                else:
                    db.execute("DELETE FROM source_elements WHERE id=?", (element_id,))
            return original(self, references, siblings, event)

        monkeypatch.setattr(check_module.GlobalCitationCheck, "_read_current", mutate_then_read)
        result = _chunk_answer(repo, notebooks, user_id)
        assert overlay
        assert result.status == "done", result.error
        assert result.answer.answer.startswith("低温性能如下。")
        cited = [r for r in kit.references(result.answer) if r.element_id == element_id]
        assert cited and {r.verification for r in cited} == {expected}
        assert getattr(result.answer.citation_check, expected) == 1
    finally:
        repo.close()
