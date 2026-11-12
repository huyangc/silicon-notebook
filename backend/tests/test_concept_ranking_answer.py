"""Counted ranking evidence remains deterministic through answer projection."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.domain.concept_ranking_contracts import RANK_FAILURE_REASONS
from app.models.ask import AskResponse, Citation, PublicTurn, QueryIntentContract
from app.services.collection_enumeration_answer import CollectionKeyAllocator, enumeration_prompt_block
from app.services.concept_ranking import (
    ConceptRankComputation,
    ConceptRankEvidence,
    ConceptRankPage,
    ConceptRankRow,
    LibraryRankCoverage,
    LibraryRanking,
)
from app.services.concept_ranking_answer import (
    rank_citation_item_id,
    rank_citation_items,
    ranking_line_rendered,
    ranking_notice,
    ranking_prompt_block,
    ranking_result_sets,
)
from app.services.concept_ranking_paging import rank_next_cursor, rank_page_rows
from app.services.context_spans import recorded_spans, recording_spans
from app.services.conversation_public_view import public_turn
from app.services.reasoning_rank_actions import ConceptRankingOutcome
from app.services.response_drafting import DefaultResponseDraftStage
from tests.test_typed_collection_result_sets import _outcome as enumeration_outcome


def outcome(*, count=1, fingerprint=None, nb="nb", pages=(0,), **coverage):
    fingerprint = fingerprint or ("fp" if nb == "nb" else f"fp-{nb}")
    rows = tuple(ConceptRankRow(
        nb, f"c{i}", f"概念{i}", count - i, count - i + 1, "g1" if i < 2 else "",
        (ConceptRankEvidence(f"e{i}", f"s{i}", nb),), "personal",
    ) for i in range(count))
    base = dict(notebook_id=nb, notebook_name="示例库", status="ok", path="scope_full",
                scope_sources=100, exact=True)
    base.update(coverage)
    library = LibraryRanking(LibraryRankCoverage(**base), rows)
    computation = ConceptRankComputation(fingerprint, "", "all", (library,), ((nb, 1, 1),), "digest")
    return ConceptRankingOutcome(fingerprint, "", "all", computation,
                                 rank_page_rows(computation, pages[-1]), pages, {},
                                 tuple(ConceptRankPage(computation, page, 20, rank_next_cursor(computation, page))
                                       for page in pages) if rows or base["status"] == "ok" else ())


def with_computation(ranked, computation):
    pages = tuple(ConceptRankPage(computation, page.page_number, page.page_size,
                                  rank_next_cursor(computation, page.page_number, page.page_size))
                  for page in ranked.successful_pages)
    return replace(ranked, fingerprint=computation.fingerprint, computation=computation,
                   successful_pages=pages, last_page_rows=pages[-1].page_rows() if pages else ())


def citations_for(*outcomes):
    return {item.object_id: Citation(label="来源", source_id=f"s{index}",
                                    element_id=item.evidence_element_ids[0],
                                    quoted_span="提及证据", location_label="p1")
            for index, item in enumerate(rank_citation_items(outcomes))}


def preview(*outcomes, keys=None, budget=100_000, citations=None):
    return ranking_prompt_block(outcomes, keys=keys or CollectionKeyAllocator(),
                                budget_chars=budget,
                                citations=citations if citations is not None else citations_for(*outcomes))


def test_shared_namespace_is_continuous_and_evidence_spans_are_registered():
    keys = CollectionKeyAllocator()
    enumeration = enumeration_prompt_block([enumeration_outcome()], inline_rows=20,
                                          budget_chars=10_000, keys=keys)
    assert set(enumeration.evidence_by_id) == {"k5001"}
    with recording_spans():
        ranking = preview(outcome(count=2), keys=keys)
        assert set(ranking.evidence_by_id) == {"k5002", "k5003"}
        spans = recorded_spans(ranking.text)
        assert {span.key for span in spans if span.key} == {"k5002", "k5003"}
        assert all(ranking.text[span.start:span.end].startswith(f"[{span.key}]")
                   for span in spans if span.key)
    with pytest.raises(ValueError):
        CollectionKeyAllocator(5999).take(2)


def test_inline_latest_page_only_and_per_library_row_and_character_limits():
    first = outcome(count=40, pages=(0, 1))
    second = outcome(count=20, fingerprint="two", nb="two")
    third = outcome(count=1, fingerprint="three", nb="three")
    result = preview(first, second, third)
    assert len(result.evidence_by_id) == 40
    assert result.inlined_fingerprints == {"fp", "two", "three"}
    assert "另有 1 行未内联。" in result.text
    assert "[k5001] 概念20" in result.text
    keys = CollectionKeyAllocator()
    assert not preview(first, keys=keys, budget=100).text
    assert keys.next_key == 5001
    assert not preview(first, citations={}).evidence_by_id


def test_cards_include_all_fetched_pages_and_only_registered_citations():
    ranked = outcome(count=60, pages=(0, 1))
    citations = citations_for(ranked)
    del citations[rank_citation_item_id("nb", "c1", 0)]
    card = ranking_result_sets([ranked], citations)[0]
    assert len(card.sections[0].rows) == 40
    assert card.sections[0].rows[0].citations
    assert not card.sections[0].rows[1].citations
    assert card.sections[0].coverage.next_cursor
    response = AskResponse(conclusion="结果", result_sets=[card])
    assert AskResponse.model_validate(response.model_dump()).result_sets[0].kind == "concept_ranking"


@pytest.mark.parametrize("reason", RANK_FAILURE_REASONS)
def test_failures_always_render_with_one_fallback_reason_and_text_only_cards(reason):
    kwargs = {"skipped": "timeout"} if reason == "library_skipped" else {"reason": reason}
    ranked = outcome(count=0, status="unavailable", exact=False, **kwargs)
    notice, failures = ranking_notice([ranked], cited_keys=frozenset(), key_owner={}, query_candidates=100)
    assert failures[0].reason == reason
    assert failures[0].line == notice
    assert bool(failures[0].fallback_line) == (reason == "not_computed")
    card = ranking_result_sets([ranked], {})[0].sections[0]
    assert not card.rows
    assert card.notice and "下面的回答" not in card.notice
    assert ("下面的回答" in notice) == (reason != "library_skipped")
    assert card.notice == {
        "scoped_large_notebook": "没能按提及次数排行：本笔记本内容较多，只勾选部分来源时不支持这类统计。",
        "not_computed": "没能按提及次数排行：本笔记本还没有排行数据。",
        "analysis_unavailable": "没能按提及次数排行：本笔记本暂时不支持这类统计。",
        "refresh_produced_nothing": "没能按提及次数排行：这次更新没有产出排行数据。",
        "recount_budget_exhausted": "没能按提及次数排行：统计超时。",
        "rank_failed": "没能按提及次数排行：统计时出错。",
        "library_skipped": "有 1 个笔记本读取失败，没有计入排行。",
    }[reason]


def test_skipped_library_grouping_and_card_order():
    failures = [outcome(nb=nb, count=0, status="unavailable", skipped="timeout", exact=False)
                for nb in ("a", "b", "a")]
    notice, grouped = ranking_notice(failures, cited_keys=frozenset(), key_owner={}, query_candidates=100)
    assert notice == "有 2 个笔记本读取失败，没有计入排行。"
    assert grouped[0].notebook_ids == ["a", "b"]
    assert DefaultResponseDraftStage._reasoning_result_sets(
        SimpleNamespace(result_sets=["knowhow"]), ["collections"], ["ranking"], ["sheets"]
    ) == ["knowhow", "collections", "ranking", "sheets"]


def test_failure_order_grouping_and_disclosure_requires_reference():
    failed = [outcome(count=0, nb=f"n{i}", status="unavailable", reason=reason, exact=False)
              for i, reason in enumerate(reversed(RANK_FAILURE_REASONS[:-1]))]
    exact = outcome()
    notice, failures = ranking_notice(failed + [exact], cited_keys=frozenset(), key_owner={}, query_candidates=100)
    assert [failure.reason for failure in failures] == list(RANK_FAILURE_REASONS[:-1])
    assert "按提及它" not in notice
    notice, _ = ranking_notice(failed + [exact], cited_keys=frozenset({"k5001"}),
                               key_owner={"k5001": ("nb", "c0")}, query_candidates=100)
    assert notice.endswith("示例库：按提及它的来源篇数统计，范围：100 篇来源。")
    assert not ranking_line_rendered({"k5001"}, {"k5001": ("nb", "c0")}, failed + [exact])


def test_all_coverage_gaps_and_duplicate_import_disclosure_in_order():
    ranked = outcome(path="query", exact=False, drifted=True, candidates_lexical_only=True,
                     stale_counts=True, seq_behind=3, recount_capped=True, recount_skipped=4,
                     unnamed_dropped=5, duplicate_import_pairs=6)
    notice, _ = ranking_notice([ranked], cited_keys=frozenset({"k5001"}),
                               key_owner={"k5001": ("nb", "c0")}, query_candidates=100)
    fragments = ["来源有变化", "只按字面", "100 个概念", "3 次变更", "4 个候选", "5 个概念", "6 对疑似"]
    assert [notice.index(text) for text in fragments] == sorted(notice.index(text) for text in fragments)
    assert not ranking_line_rendered({"k5001"}, {"k5001": ("nb", "c0")}, [ranked])


def draft_conclusion(outcomes, *, cited=True, completeness=True, knowhow=False):
    stage = object.__new__(DefaultResponseDraftStage)
    stage.settings = SimpleNamespace(kg_rank_query_candidates=100)
    values = dict(answer="正文[k5001]" if cited else "正文", grounded=True, synth_failed=False,
                  typed_collection_result_sets=[], paper_evidence=None,
                  structured_map={"k5001": {"notebook_id": "nb", "object_id": "c0",
                                           "provenance": {"producer": "concept_ranking"}}})
    batch = SimpleNamespace(returned_rows=1, known_total_rows=1, complete=True,
                            synthesis_complete=True, synthesis_rows=1) if knowhow else None
    run = SimpleNamespace(stage=SimpleNamespace(rankings=outcomes), top_hits=[],
                          intent_contract=QueryIntentContract(objective="排行", resolved_question="排行",
                                                               result_scope="aggregate"),
                          completeness_unavailable=completeness, structured_batch=batch,
                          ready=lambda key: values[key])
    stage._draft_conclusion(run)
    return run


def test_exact_ranking_suppresses_aggregate_warning_but_only_when_cited():
    assert not draft_conclusion([outcome()]).warning
    assert draft_conclusion([outcome()], cited=False).warning
    assert draft_conclusion([outcome(exact=False, drifted=True)]).warning


def test_same_library_s_and_q_calls_share_one_conservative_disclosure():
    exact = outcome(duplicate_import_pairs=2)
    query = outcome(fingerprint="query", path="query", exact=False, duplicate_import_pairs=2)
    run = draft_conclusion([exact, query])
    assert run.warning
    assert run.ranking_notice.count("按提及它的来源篇数统计") == 1
    assert "100 个概念里排行" in run.ranking_notice
    assert "范围：100 篇来源" not in run.ranking_notice
    assert run.ranking_notice.count("全库有 2 对") == 1
    assert "\n" not in run.ranking_notice


def test_same_library_keeps_distinct_gap_counts_in_fixed_field_order():
    first = outcome(fingerprint="first", exact=False, path="precomputed", stale_counts=True,
                    seq_behind=3, unnamed_dropped=7, duplicate_import_pairs=2)
    second = outcome(fingerprint="second", exact=False, path="query", drifted=True,
                     candidates_lexical_only=True, stale_counts=True, seq_behind=5,
                     recount_capped=True, recount_skipped=4, unnamed_dropped=9,
                     duplicate_import_pairs=6)
    run = draft_conclusion([first, second])
    assert run.warning
    assert "\n" not in run.ranking_notice
    fragments = ["来源有变化", "只按字面", "100 个概念", "3 次变更", "5 次变更",
                 "4 个候选", "7 个概念", "9 个概念", "2 对疑似", "6 对疑似"]
    positions = [run.ranking_notice.index(text) for text in fragments]
    assert positions == sorted(positions)


def test_failure_disclosure_knowhow_and_model_order_and_warning_tail():
    failed = outcome(count=0, nb="failed", reason="not_computed", status="unavailable", exact=False)
    run = draft_conclusion([outcome(), failed], completeness=False, knowhow=True)
    assert run.answer.index("没能按提及") < run.answer.index("按提及它") < run.answer.index("Knowhow 枚举") < run.answer.index("正文")
    run = draft_conclusion([outcome(), failed])
    assert run.answer.endswith("本次回答未验证完整性，不能视为全部结果。")
    assert draft_conclusion([failed], cited=False).answer.startswith("> 没能按提及")


def test_public_turn_keeps_ranking_notice_and_omits_ranking_card():
    notice = "按提及它的来源篇数统计，范围：100 篇来源。"
    payload = AskResponse(conclusion="正文", answer=f"> {notice}\n\n正文", ranking_notice=notice,
                          result_sets=ranking_result_sets([outcome()], {})).model_dump()
    public = public_turn({"question": "排行", "payload": payload}, share_token="token", images_enabled=False)
    assert public["ranking_notice"] == notice
    assert public["omitted_result_sets"]
    assert PublicTurn.model_validate(public).ranking_notice == notice


def test_ranking_keeps_large_outline_in_single_synthesis(monkeypatch):
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.core.config import Settings
    from app.services import outline_synthesis, reasoning_retrieval

    stage = object.__new__(DefaultResponseDraftStage)
    stage.settings = Settings()
    monkeypatch.setattr(reasoning_retrieval, "outline_wiring_active", lambda *_: True)
    monkeypatch.setattr(outline_synthesis, "plan_outline_sections", lambda *_, **__: ([object(), object()], []))
    monkeypatch.setattr(stage, "_answer_reasoning_sections", lambda *_, **__: pytest.fail("sectioned ranking"))
    values = {"answer_client": SimpleNamespace(configured=True), "admitted_external": set(), "run_lookups": None}
    run = SimpleNamespace(
        stage=SimpleNamespace(rankings=[outcome()]), limits=ask_retrieval_limits("exhaustive"),
        report=None, notebook_id="nb", research_question="排行", reasoning_history=[], style_block="",
        top_hits=[], elements=[], chunks=[], enumerations=[], structured_batch=None,
        reasoning_outline=[object(), object()], reasoning_outline_evidence=[], cancel_event=None,
        err_sink=[], record_section_step=lambda *_: None, ready=lambda key: values[key],
    )
    stage._draft_sectioned_attempt(run)
    assert run.outline_planned and not run.outline_attempted and run.sectioned is None


def test_ranking_only_evidence_runs_one_synthesis(tmp_path, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningResult
    from tests import global_citation_e2e_kit as kit
    from tests.test_paper_card_answer import _ask

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s0", "概念资料", ["概念0的提及记录"])
        ranked = outcome(nb=nb.id)
        row = replace(ranked.last_page_rows[0], evidence=(ConceptRankEvidence("s0-000", "s0", nb.id),))
        library = replace(ranked.computation.libraries[0], rows=(row,))
        ranked = with_computation(ranked, replace(ranked.computation, libraries=(library,)))
        result = ReasoningResult(rankings=[ranked])
        response, llm = _ask(repo, nb.id, result, monkeypatch,
                             answer=lambda _: "概念0在1篇来源中出现[k5001]。")
        assert len(llm.prompts) == 1
        assert "[k5001] 概念0 — 在 1 篇来源中出现，共 2 条" in llm.prompts[0]
        assert response.answer.endswith("概念0在1篇来源中出现[k5001]。")
        assert response.ranking_notice
        assert any(anchor.key == "k5001" for anchor in response.anchors)
    finally:
        repo.close()


def test_failed_ranking_header_reaches_answer_prompt_and_evidence(tmp_path, monkeypatch):
    from app.models.schemas import AskRequest
    from app.services.reasoning_retrieval import ReasoningResult
    from tests import global_citation_e2e_kit as kit
    from tests.test_paper_card_answer import _ask

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s0", "资料", ["原文"])
        ranked = outcome(nb=nb.id, count=0, status="unavailable", reason="rank_failed", exact=False)
        response, llm = _ask(repo, nb.id, ReasoningResult(rankings=[ranked]), monkeypatch,
                             answer=lambda _: "目前无法统计。")
        assert "没能按提及次数排行：统计时出错。" in llm.prompts[0]
        assert "Counting did not complete; do not infer" in llm.prompts[0]
        assert response.ranking_failures[0].reason == "rank_failed"
        evidence = repo._runtime.ask_service().ask_evidence(
            nb.id, AskRequest(question="统计概念", mode="reasoning"), user_id=repo.current_user().id)
        text = "\n".join(item.text for item in evidence.items)
        assert "没能按提及次数排行：统计时出错。" in text
        assert "Counting did not complete; do not infer" in text
        assert not any(item.key for item in evidence.items)
    finally:
        repo.close()


def test_citation_projection_failure_preserves_answer_and_failure_notice(tmp_path, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningResult
    from tests import global_citation_e2e_kit as kit
    from tests.test_paper_card_answer import _ask

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s0", "资料", ["原文"])
        service = repo._runtime.ask_service()
        events = []
        service._response_drafting.emit_event = events.append
        def fail(*args, **kwargs):
            raise ValueError("private source text must not enter telemetry")
        monkeypatch.setattr(service.evidence_context, "collection_item_citations", fail)
        failed = outcome(nb=nb.id, count=0, status="unavailable", reason="rank_failed", exact=False)
        response, llm = _ask(repo, nb.id, ReasoningResult(rankings=[failed]), monkeypatch,
                             answer=lambda _: "读取失败后仍然可以回答。")
        assert len(llm.prompts) == 1
        assert response.answer.endswith("读取失败后仍然可以回答。")
        assert not response.result_sets
        assert response.ranking_failures[0].reason == "rank_failed"
        assert events == [{"kind": "concept_ranking_projection_failed", "error_type": "ValueError"}]
    finally:
        repo.close()


@pytest.mark.parametrize("exception", ["cancel", "control"])
def test_ranking_projection_preserves_control_exceptions(exception):
    from app.domain.cancellation import AskCancelled
    from app.domain.retrieval_control import RetrievalControlError

    error = AskCancelled() if exception == "cancel" else RetrievalControlError()
    def fail(*args, **kwargs):
        raise error
    service = object.__new__(DefaultResponseDraftStage)
    service.evidence_context = SimpleNamespace(collection_item_citations=fail)
    with pytest.raises(type(error)):
        service._add_ranking_block("", {}, [outcome()], CollectionKeyAllocator(),
                                   SimpleNamespace(configured=True),
                                   SimpleNamespace(chunk_context_chars=10000), None,
                                   notebook_id="nb", memo={})


def test_failure_suffix_only_without_any_ranking_reference_and_latest_chain_wins():
    failed = outcome(count=0, status="unavailable", reason="rank_failed", exact=False)
    success = outcome()
    other = outcome(nb="other")
    plain, _ = ranking_notice([failed], cited_keys=frozenset(), key_owner={}, query_candidates=100)
    assert "下面的回答" in plain
    mixed, failures = ranking_notice([failed, other], cited_keys=frozenset({"k5001"}),
                                     key_owner={"k5001": ("other", "c0")}, query_candidates=100)
    assert "下面的回答" not in mixed
    assert failures[0].line == "没能按提及次数排行：统计时出错。"
    recovered, failures = ranking_notice([failed, success], cited_keys=frozenset({"k5001"}),
                                         key_owner={"k5001": ("nb", "c0")}, query_candidates=100)
    assert not failures and "没能" not in recovered
    assert ranking_line_rendered({"k5001"}, {"k5001": ("nb", "c0")}, [failed, success])
    assert len(ranking_result_sets([failed, success], {})) == 1


def test_ranking_budget_preserves_paper_digest_and_document_read(monkeypatch):
    from app.services import document_read_answer, response_drafting

    ranked = outcome(count=20)
    rows = tuple(replace(row, name=row.name + "长" * 1000) for row in ranked.last_page_rows)
    ranked = with_computation(ranked, replace(ranked.computation, libraries=(replace(
        ranked.computation.libraries[0], rows=rows),)))
    refs = citations_for(ranked)
    service = object.__new__(DefaultResponseDraftStage)
    service.evidence_context = SimpleNamespace(collection_item_citations=lambda *_, **__: refs)
    service.logger = SimpleNamespace(warning=lambda *args: None)
    service.emit_event = None
    monkeypatch.setattr(response_drafting, "document_read_block_reserve", lambda reads: 3002)
    monkeypatch.setattr(response_drafting, "paper_digest_reserve", lambda paper, budget: 3002)
    monkeypatch.setattr(response_drafting, "paper_digest_block",
                        lambda paper, budget: ("P" * 3000, {}) if budget >= 3000 else ("", {}))
    monkeypatch.setattr(document_read_answer, "document_read_prompt_block", lambda *_, **__:
                        SimpleNamespace(text="D" * 3000, evidence_by_id={}, citations=[]))
    budgets = []
    real_preview = response_drafting.ranking_prompt_block
    def captured(*args, **kwargs):
        budgets.append(kwargs["budget_chars"])
        return real_preview(*args, **kwargs)
    monkeypatch.setattr(response_drafting, "ranking_prompt_block", captured)
    stage = SimpleNamespace(prepared=SimpleNamespace(
        notebook_id="nb", limits=SimpleNamespace(chunk_context_chars=30000)),
        structured_batch=None, enumerations=[], rankings=[ranked], spreadsheet_results=[],
        document_reads=[object()])
    rendered = service._render_structured_side(stage, SimpleNamespace(configured=True), None, {}, object())
    assert budgets == [15000 - 3002 - 3002]
    assert rendered[1] and "另有" in rendered[0]
    assert "P" * 3000 in rendered[0] and "D" * 3000 in rendered[0]
    assert not rendered[5]
    assert len(rendered[0]) <= 15000


def test_uncitable_rows_keep_counts_without_keys_or_failure_instruction():
    keys = CollectionKeyAllocator()
    result = preview(outcome(count=2), keys=keys, citations={})
    assert "按提及它的来源篇数统计" in result.text
    assert "Counting did not complete" not in result.text
    assert "No concept in scope matched" not in result.text
    assert "概念0 — 在 2 篇来源中出现，共 3 条" in result.text
    assert "概念1 — 在 1 篇来源中出现，共 2 条" in result.text
    assert "[k" not in result.text
    assert not result.evidence_by_id and keys.next_key == 5001


def test_partial_inline_reserves_later_library_headers_and_contiguous_keys():
    first = outcome(count=20)
    second = outcome(count=1, nb="other")
    other_library = replace(second.computation.libraries[0], coverage=replace(
        second.computation.libraries[0].coverage, notebook_name="第二库"))
    first = with_computation(first, replace(first.computation,
                                           libraries=first.computation.libraries + (other_library,)))
    keys = CollectionKeyAllocator()
    result = preview(first, keys=keys, budget=750)
    assert result.evidence_by_id
    assert "示例库：" in result.text and "第二库：" in result.text
    assert "另有" in result.text
    assert keys.next_key == 5001 + len(result.evidence_by_id)
    assert list(result.evidence_by_id) == [f"k{5001 + i}" for i in range(len(result.evidence_by_id))]


def many_libraries(count, *, rows=20, pages=(0,), fingerprint="many"):
    libraries = tuple(outcome(count=rows, nb=f"nb{i}").computation.libraries[0]
                      for i in range(count))
    computation = ConceptRankComputation(fingerprint, "", "all", libraries,
                                         tuple((f"nb{i}", 1, 1) for i in range(count)), "digest")
    return ConceptRankingOutcome(fingerprint, "", "all", computation,
                                 rank_page_rows(computation, pages[-1]), pages, {},
                                 tuple(ConceptRankPage(computation, page, 20, rank_next_cursor(computation, page))
                                       for page in pages))


def test_three_library_cards_and_inline_share_seven_seven_six_page_quotas():
    ranked = many_libraries(3, pages=(0, 1))
    refs = citations_for(ranked)
    card = ranking_result_sets([ranked], refs)[0]
    assert [len(section.rows) for section in card.sections] == [14, 14, 12]
    assert [[row.name for row in section.rows] for section in card.sections] == [
        [f"概念{i}" for i in range(14)], [f"概念{i}" for i in range(14)],
        [f"概念{i}" for i in range(12)],
    ]
    block = preview(ranked, citations=refs)
    by_library = {}
    for row in block.evidence_by_id.values():
        by_library.setdefault(row["notebook_id"], []).append(row["object_id"])
    assert by_library == {
        "nb0": [f"c{i}" for i in range(7, 14)],
        "nb1": [f"c{i}" for i in range(7, 14)],
        "nb2": [f"c{i}" for i in range(6, 12)],
    }
    assert len(block.evidence_by_id) == 20
    assert len({section.coverage.next_cursor for section in card.sections}) == 1
    assert card.sections[0].coverage.next_cursor


def test_twenty_five_libraries_keep_one_row_each_per_page_and_share_inline_cap():
    ranked = many_libraries(25, rows=2, pages=(0, 1))
    refs = citations_for(ranked)
    card = ranking_result_sets([ranked], refs)[0]
    assert [len(section.rows) for section in card.sections] == [2] * 25
    assert all([row.name for row in section.rows] == ["概念0", "概念1"] for section in card.sections)
    assert not any(section.coverage.next_cursor for section in card.sections)
    block = preview(ranked, citations=refs)
    assert len(block.evidence_by_id) == 25
    assert {row["notebook_id"] for row in block.evidence_by_id.values()} == {f"nb{i}" for i in range(25)}
    assert {row["object_id"] for row in block.evidence_by_id.values()} == {"c1"}
    second = with_computation(ranked, replace(ranked.computation, fingerprint="second"))
    keys = CollectionKeyAllocator()
    combined = preview(ranked, second, keys=keys, citations=refs)
    assert len(combined.evidence_by_id) == 40 and keys.next_key == 5041
    assert combined.text.count("另有 1 行未内联。") == 10


def test_successful_empty_result_is_not_a_counting_failure():
    ranked = outcome(count=0)
    keys = CollectionKeyAllocator()
    block = preview(ranked, keys=keys)
    assert "范围：100 篇来源" in block.text
    assert "No concept in scope matched this request" in block.text
    assert "Counting did not complete" not in block.text
    assert not block.evidence_by_id and keys.next_key == 5001
    assert ranking_notice([ranked], cited_keys=frozenset(), key_owner={}, query_candidates=100) == ("", [])


def test_exhausted_library_page_does_not_claim_no_matching_concepts():
    ranked = many_libraries(2, rows=80, pages=(0, 1))
    first_library, second_library = ranked.computation.libraries
    computation = replace(ranked.computation, libraries=(
        replace(first_library, rows=first_library.rows[:2]), second_library))
    ranked = with_computation(ranked, computation)
    refs = citations_for(ranked)
    card = ranking_result_sets([ranked], refs)[0]
    assert [len(section.rows) for section in card.sections] == [2, 20]
    block = preview(ranked, citations=refs)
    assert block.text.count("范围：100 篇来源") == 2
    assert {row["notebook_id"] for row in block.evidence_by_id.values()} == {"nb1"}
    assert len(block.evidence_by_id) == 10
    assert "No concept in scope matched this request" not in block.text
    assert "Counting did not complete" not in block.text


def test_unreferenced_rows_use_the_shared_row_budget_but_no_keys():
    first = outcome(count=20)
    second = outcome(count=20, fingerprint="second")
    third = outcome(count=2, fingerprint="third")
    keys = CollectionKeyAllocator()
    block = preview(first, second, third, keys=keys, citations={})
    assert block.text.count(" — 在 ") == 40
    assert "另有 2 行未内联。" in block.text
    assert keys.next_key == 5001 and not block.evidence_by_id
    assert "Counting did not complete" not in block.text


def test_uncitable_rows_do_not_create_holes_in_mixed_citation_keys():
    ranked = outcome(count=3)
    refs = citations_for(ranked)
    del refs[rank_citation_item_id("nb", "c0", 0)]
    del refs[rank_citation_item_id("nb", "c2", 0)]
    keys = CollectionKeyAllocator()
    block = preview(ranked, keys=keys, citations=refs)
    assert "\n概念0 — " in block.text and "\n概念2 — " in block.text
    assert "[k5001] 概念1 — " in block.text
    assert list(block.evidence_by_id) == ["k5001"] and keys.next_key == 5002


def test_cards_keep_actual_old_page_rows_when_recovery_changes_library_quota():
    ranked = many_libraries(3)
    previous = ranked.successful_pages[0]
    changed = replace(ranked.computation, libraries=(ranked.computation.libraries[0],
                                                    ranked.computation.libraries[2]))
    recovered = ConceptRankPage(changed, 1, 20, rank_next_cursor(changed, 1))
    ranked = replace(ranked, computation=changed, fetched_pages=(0, 1),
                     successful_pages=(previous, recovered), last_page_rows=recovered.page_rows())
    cards = ranking_result_sets([ranked], citations_for(ranked))[0]
    assert [section.coverage.notebook_id for section in cards.sections] == ["nb0", "nb1", "nb2"]
    assert [[row.name for row in section.rows] for section in cards.sections] == [
        [f"概念{i}" for i in (*range(7), *range(10, 20))],
        [f"概念{i}" for i in range(7)],
        [f"概念{i}" for i in (*range(6), *range(10, 20))],
    ]


def test_partial_library_failure_keeps_its_old_card_inline_and_coverage():
    ranked = many_libraries(2, rows=40)
    failed = replace(ranked.computation.libraries[0], rows=(), coverage=replace(
        ranked.computation.libraries[0].coverage, status="unavailable", skipped="timeout", exact=False))
    computation = replace(ranked.computation, libraries=(failed, ranked.computation.libraries[1]))
    second = ConceptRankPage(computation, 1, 20, "")
    ranked = replace(ranked, computation=computation, fetched_pages=(0, 1),
                     successful_pages=ranked.successful_pages + (second,), last_page_rows=second.page_rows())
    refs = citations_for(ranked)
    card = ranking_result_sets([ranked], refs)[0]
    assert [len(section.rows) for section in card.sections] == [10, 30]
    assert card.sections[0].coverage.exact and card.sections[0].rows[0].citations
    block = preview(ranked, citations=refs)
    assert len(block.evidence_by_id) == 30
    old = [row["object_id"] for row in block.evidence_by_id.values() if row["notebook_id"] == "nb0"]
    assert old == [f"c{i}" for i in range(10)]
    assert "读取失败" in block.text and "Counting did not complete" in block.text
    owner = {key: (row["notebook_id"], row["object_id"]) for key, row in block.evidence_by_id.items()}
    notice, failures = ranking_notice([ranked], cited_keys=frozenset(owner), key_owner=owner, query_candidates=100)
    assert failures[0].reason == "library_skipped"
    assert "范围：100 篇来源" in notice
    assert not ranking_line_rendered(frozenset(owner), owner, [ranked])


def test_real_answer_keeps_first_page_after_second_page_timeout(tmp_path, monkeypatch):
    from app.models.schemas import AskRequest
    from tests import global_citation_e2e_kit as kit

    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        nb = kit.notebook(repo)
        kit.seed_document(repo, nb.id, "s0", "概念资料", ["概念的提及记录"])
        repo.store_kg(nb.id, "s0", [{"local_id": "C1", "object_type": "concept",
            "payload": {"name": "概念0"}, "evidence": [kit.evidence("s0", "s0-000", "概念")]}], [])
        ranked = outcome(nb=nb.id, count=25)
        rows = tuple(replace(row, evidence=(ConceptRankEvidence("s0-000", "s0", nb.id),))
                     for row in ranked.computation.libraries[0].rows)
        ranked = with_computation(ranked, replace(ranked.computation, libraries=(replace(
            ranked.computation.libraries[0], rows=rows),)))
        first = ranked.successful_pages[0]
        failed_library = LibraryRanking(replace(first.computation.libraries[0].coverage,
                                                status="unavailable", skipped="timeout", exact=False), ())
        failed = ConceptRankPage(replace(first.computation, libraries=(failed_library,)), 1, 20, "")
        calls = []
        def rank(_notebook_id, request, **kwargs):
            calls.append(request)
            return failed if request.cursor else first
        service = repo._runtime.ask_service()
        monkeypatch.setattr(service.concept_ranking, "rank", rank)
        llm = kit.bind_models(repo, kit.ScriptedAgent([
            {"next_action": "rank_concepts", "rank_concepts": {"query": ""}},
            {"next_action": "rank_concepts", "rank_concepts": {"query": "", "cursor": "c1:p1"}},
        ]))
        response = service.ask_reasoning(nb.id, AskRequest(question="统计概念", mode="reasoning"),
                                         user_id=repo.current_user().id)
        assert len(calls) == 2 and calls[1].cursor
        card = next(card for card in response.result_sets if card.kind == "concept_ranking")
        assert len(card.sections[0].rows) == 20 and card.sections[0].rows[0].citations
        assert card.sections[0].coverage.exact
        assert response.answer.startswith("> 有 1 个笔记本读取失败，没有计入排行。")
        assert "概念0 — 在 25 篇来源中出现，共 26 条" in llm.prompts[-1]
        assert any(anchor.provenance.get("producer") == "concept_ranking" for anchor in response.anchors)
    finally:
        repo.close()
