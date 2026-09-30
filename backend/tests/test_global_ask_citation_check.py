"""PR-D -- the global terminal citation check, its copy, projection and twins.

The engine-level behaviour (delivered whole, marked per citation, counts,
events, trace step) is pinned in ``test_global_ask_engine_parity.py``; this
file pins the pieces that other surfaces consume directly -- the notice copy,
the card labels, the ``global_answer_check`` projection -- and the two
real-repository properties on SQLite: one digest definition shared with the
store, and the federated passage producer racing the terminal read. The
PostgreSQL twins live in ``tests/postgres/test_global_citation_race_pg.py``.
"""
from __future__ import annotations

import hashlib

import pytest

from app.core.config import Settings
from app.domain.evidence_fingerprint import element_text_sha
from app.models.ask import AskResponse, Citation
from app.models.global_ask import GlobalAskJob
from app.services.global_citation_check import (
    VERIFICATION_LABELS, citation_check_notice, global_answer_check,
)
from tests.citation_check_testkit import (
    RACE_EXPECTATIONS, TWIN_TEXTS, passage_race, seed_source,
)


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def _summary(**counts):
    failed = sum(counts.values())
    return {"outcome": "partial", "checked": failed + 1, "failed": failed,
            "changed": 0, "source_gone": 0, "unverifiable": 0, **counts}


_TAIL = "回答内容照常保留，带标记的引用可点开查看原因。"


@pytest.mark.parametrize("counts,clauses", [
    ({"changed": 2}, "2 条原文已改动"),
    ({"source_gone": 1}, "1 条资料已删除"),
    ({"unverifiable": 3}, "3 条无法核对"),
    ({"changed": 1, "source_gone": 1, "unverifiable": 1},
     "1 条原文已改动、1 条资料已删除、1 条无法核对"),
], ids=["changed", "source-gone", "unverifiable", "all-three"])
def test_the_notice_is_one_sentence_with_a_clause_per_reason(counts, clauses):
    """Q3: one sentence under the answer, one clause per failed kind, in the
    frontend's canonical wording (``citation-verification.ts``)."""
    assert citation_check_notice(_summary(**counts)) == (
        f"本次回答有部分引用未通过核对：{clauses}。{_TAIL}"
    )


def test_the_snapshot_notice_is_in_the_past_tense():
    assert citation_check_notice(_summary(changed=1), tense="snapshot") == (
        f"回答生成时，有部分引用未通过核对：1 条原文已改动。{_TAIL}"
    )


def test_the_notice_falls_back_to_the_total_without_per_kind_counts():
    """Never an empty colon: an old or foreign shape still states how many."""
    assert citation_check_notice({"failed": 2}) == (
        f"本次回答有部分引用未通过核对：共 2 条。{_TAIL}"
    )


def test_no_notice_when_nothing_failed():
    assert citation_check_notice(None) == ""
    assert citation_check_notice({"failed": 0}) == ""


def test_card_labels():
    assert VERIFICATION_LABELS == {
        "changed": "原文已改动", "source_gone": "资料已删除", "unverifiable": "无法核对",
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _job(answer=None, response=None):
    return GlobalAskJob.model_validate({
        "job_id": "job", "conversation_id": "conversation", "status": "done",
        "question": "q", "created_at": "2026-09-29T00:00:00Z",
        "notebook_scope": {"mode": "all"}, "resolved_notebook_ids": ["a"],
        "answer": answer, "response": response,
    })


def test_global_answer_check_reads_the_model_and_the_stored_row():
    summary = _summary(changed=1)
    answer = {"conclusion": "c", "answer": "a", "citation_check": summary}
    assert global_answer_check(_job(answer)) == summary
    assert global_answer_check({"payload": {"answer": answer}}) == summary
    assert global_answer_check({"answer": answer}) == summary


def test_global_answer_check_is_none_without_a_failure():
    assert global_answer_check(_job({"conclusion": "c", "answer": "a"})) is None
    assert global_answer_check({"payload": {"answer": {"answer": "a"}}}) is None
    assert global_answer_check({"payload": {}}) is None
    assert global_answer_check(_job()) is None


def test_a_row_voided_in_the_past_keeps_its_stored_sentence():
    """No migration: a row the old check voided reads back exactly as stored and
    carries no check summary -- it is not re-judged after the fact."""
    voided = "引用原文在回答期间发生了变化，暂时无法提供可靠结论，请重新提问。"
    stored = {"conclusion": voided, "answer": voided, "grounded": False,
              "evidence_level": "inferred", "citations": [], "anchors": []}
    job = _job(stored)
    assert job.answer.answer == voided
    assert global_answer_check(job) is None
    assert "citation_check" not in job.answer.model_dump(mode="json")


def test_single_notebook_serialisation_is_unchanged():
    """The new fields are absent unless a global check set them."""
    response = AskResponse(conclusion="c", citations=[Citation(
        label="l", source_id="s", element_id="e", location_label="", quoted_span="q",
    )])
    dumped = response.model_dump(mode="json")
    assert "citation_check" not in dumped
    assert "verification" not in dumped["citations"][0]


# ---------------------------------------------------------------------------
# SQLite twin + race (real repository)
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_store(tmp_path):
    from app.models.notebooks import NotebookCreate
    from app.repositories.sqlite.source_store import SourceStore
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'twin.db'}",
        storage_dir=str(tmp_path / "storage"),
    ))
    notebook = repo.create_notebook(NotebookCreate(name="twin"))
    database = repo._runtime.source_store.database
    sources = object.__new__(SourceStore)
    sources.database = database
    try:
        yield sources, database, notebook.id
    finally:
        repo.close()


def test_sqlite_prints_are_the_shared_digest(sqlite_store):
    """One definition: the store's element and passage prints, the federation's
    ``_text_sha`` and the helper agree byte for byte, and no normalization makes
    a decomposed accent collide with a precomposed one."""
    from app.services.chunk_federation import _text_sha

    sources, database, notebook_id = sqlite_store
    elements = {f"el-twin-{name}": text for name, text in TWIN_TEXTS.items()}
    with database.write() as db:
        seed_source(db, "?", notebook_id=notebook_id, source_id="src-twin",
                    elements=elements)
        for name, text in TWIN_TEXTS.items():
            seed_source(db, "?", notebook_id=notebook_id, source_id=f"src-p-{name}",
                        elements={}, chunk=(f"chunk-{name}", [f"el-twin-{name}"], text))

    prints = sources.evidence_fingerprints(list(elements))
    passages = sources.passage_evidence_snapshot([f"chunk-{name}" for name in TWIN_TEXTS])
    for name, text in TWIN_TEXTS.items():
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert element_text_sha(text) == expected == _text_sha(text)
        assert prints[f"el-twin-{name}"] == ("src-twin", expected)
        assert passages[f"chunk-{name}"]["text_sha"] == expected
        assert passages[f"chunk-{name}"]["elements"][f"el-twin-{name}"] == (
            "src-twin", expected,
        )
    assert prints["el-twin-combining"] != prints["el-twin-precomposed"]
    assert element_text_sha(None) == element_text_sha("")


@pytest.mark.parametrize("mutation", sorted(RACE_EXPECTATIONS))
def test_sqlite_federated_passage_race_before_the_terminal_read(sqlite_store, mutation):
    """Real store, real producer, real terminal read: UPDATE/DELETE the cited
    element (or its passage sibling, or the whole source) after the snapshot
    and before the check. Same text re-inserted under the same id holds."""
    sources, database, notebook_id = sqlite_store

    response = passage_race(sources, database, "?", notebook_id, mutation)

    expected = RACE_EXPECTATIONS[mutation]
    assert response.answer == "答案"
    assert response.citations[0].verification == expected
    if expected is None:
        assert response.citation_check is None
        assert response.evidence_level == "grounded"
    else:
        assert getattr(response.citation_check, expected) == 1
        assert response.evidence_level == "overview"
        assert response.grounded is False


# ---------------------------------------------------------------------------
# Facts the frontend relies on
# ---------------------------------------------------------------------------

def test_apply_outcome_marks_references_and_never_touches_the_text():
    """Marks only: answer, conclusion, ``completeness_notice`` and every list are
    delivered exactly as the engine produced them (Q3)."""
    from app.models.ask import AnswerAnchor
    from app.services.global_citation_check import CitationCheckOutcome, apply_outcome

    citation = Citation(label="l", source_id="s", element_id="e", location_label="",
                        quoted_span="q", notebook_id="nb")
    anchor = AnswerAnchor(key="k1", object_id="e", object_type="element", label="l",
                          source_id="s", element_id="e", notebook_id="nb")
    response = AskResponse(
        conclusion="结论 [k1]", answer="正文 [k1]", completeness_notice="覆盖说明",
        grounded=True, evidence_level="grounded", citations=[citation], anchors=[anchor],
    )
    before = response.model_dump(exclude={"citations", "anchors", "grounded", "evidence_level"})

    apply_outcome(response, CitationCheckOutcome(
        checked=1, verdicts={("nb", "s", "e"): ("changed", "changed")},
    ))

    assert response.model_dump(
        exclude={"citations", "anchors", "grounded", "evidence_level", "citation_check"},
    ) == before
    assert response.citations[0].verification == "changed"
    assert response.anchors[0].verification == "changed"
    assert response.citation_check.model_dump() == {
        "outcome": "partial", "checked": 1, "failed": 1,
        "changed": 1, "source_gone": 0, "unverifiable": 0,
    }


def test_only_the_three_wire_values_can_be_serialised():
    from pydantic import ValidationError

    from app.models.ask import AnswerAnchor

    for value in ("changed", "source_gone", "unverifiable"):
        assert Citation(label="l", source_id="s", element_id="e", location_label="",
                        quoted_span="q", verification=value).verification == value
    with pytest.raises(ValidationError):
        Citation(label="l", source_id="s", element_id="e", location_label="",
                 quoted_span="q", verification="out_of_ceiling")
    with pytest.raises(ValidationError):
        AnswerAnchor(key="k1", object_id="e", object_type="element", label="l",
                     verification="unattested")


# ---------------------------------------------------------------------------
# Judgement table (fix round items 2, 7, 8)
# ---------------------------------------------------------------------------

def _ref(element_id="e1", source_id="s1", notebook_id="nb"):
    from types import SimpleNamespace

    return SimpleNamespace(element_id=element_id, source_id=source_id,
                           notebook_id=notebook_id)



@pytest.mark.parametrize("evidence,current,live,expected", [
    # element changed + sibling unreadable -> the definite finding wins
    ({"e1": ("s1", "a"), "e2": None}, {"e1": ("s1", "A"), "e2": ("s1", "b")},
     {"s1"}, ("changed", "changed")),
    # element unattested + sibling changed -> changed
    ({"e2": ("s1", "b")}, {"e1": ("s1", "a"), "e2": ("s1", "B")},
     {"s1"}, ("changed", "changed")),
    # sibling source gone + element changed -> source_gone
    ({"e1": ("s1", "a"), "e2": ("s2", "b")}, {"e1": ("s1", "A"), "e2": ("s2", "b")},
     {"s1"}, ("source_gone", "source_gone")),
    # element deleted + sibling changed -> source_gone
    ({"e1": ("s1", "a"), "e2": ("s1", "b")}, {"e2": ("s1", "B")},
     {"s1"}, ("source_gone", "source_gone")),
    # element unattested + sibling unreadable -> unverifiable
    ({"e2": None}, {"e1": ("s1", "a"), "e2": ("s1", "b")},
     {"s1"}, ("unverifiable", "unattested")),
    # everything holds
    ({"e1": ("s1", "a"), "e2": ("s1", "b")}, {"e1": ("s1", "a"), "e2": ("s1", "b")},
     {"s1"}, None),
], ids=["changed>unverifiable", "sibling-changed>unattested", "source_gone>changed",
        "element-gone>sibling-changed", "unattested+unreadable", "holds"])
def test_the_most_definite_finding_wins(evidence, current, live, expected):
    from app.services.global_citation_check import judge_reference

    assert judge_reference(
        _ref(), evidence=evidence, current=current, siblings=("e2",),
        ceiling={"s1", "s2"}, live_sources=live,
    ) == expected


def test_a_sibling_with_no_snapshot_fails_closed():
    """A sibling is known only because the federated channel published its
    passage and fingerprinted it in the same read; no stated snapshot is a
    contradiction and must not pass."""
    from app.services.global_citation_check import judge_reference

    assert judge_reference(
        _ref(), evidence={"e1": ("s1", "a")},
        current={"e1": ("s1", "a"), "e2": ("s1", "b")}, siblings=("e2",),
        ceiling={"s1"}, live_sources={"s1"},
    ) == ("unverifiable", "unreadable")


def test_a_citation_naming_another_source_than_its_snapshot_is_changed():
    from app.services.global_citation_check import judge_reference

    assert judge_reference(
        _ref(source_id="s1"), evidence={"e1": ("s2", "a")}, current={"e1": ("s2", "a")},
        siblings=(), ceiling={"s1", "s2"}, live_sources={"s1", "s2"},
    ) == ("changed", "changed")


def test_no_source_and_a_declared_none_records_unreadable():
    from app.services.global_citation_check import judge_reference

    assert judge_reference(
        _ref(source_id=""), evidence={"e1": None}, current={}, siblings=(),
        ceiling={"s1"}, live_sources={"s1"},
    ) == ("unverifiable", "unreadable")
    assert judge_reference(
        _ref(source_id=""), evidence={}, current={}, siblings=(),
        ceiling={"s1"}, live_sources={"s1"},
    ) == ("unverifiable", "unattested")


def test_cancellation_during_a_terminal_read_propagates():
    """The read budget polls the run's token and surfaces as its own timeout;
    the check must raise the cancellation, not mark references unreadable."""
    import threading

    from app.repositories.read_budget import ReadBudgetExceeded
    from app.services.cancellation import AskCancelled
    from app.services.global_citation_check import GlobalCitationCheck

    cancel = threading.Event()

    class _Sources:
        def evidence_fingerprints(self, ids):
            cancel.set()
            raise ReadBudgetExceeded("read budget exhausted")

        def visible_source_ids_by_notebook(self, ids):  # pragma: no cover
            raise AssertionError("must not be reached")

    response = AskResponse(conclusion="c", citations=[Citation(
        label="l", source_id="s1", element_id="e1", location_label="", quoted_span="q",
        notebook_id="nb",
    )])
    # The library is outside the ceiling, so the fingerprint read is the ONLY
    # read: no later read's own cancellation poll can mask a swallowed one.
    with pytest.raises(AskCancelled):
        GlobalCitationCheck(_Sources(), notebook_timeout_seconds=5).run(
            response, evidence={}, siblings={}, source_ceiling={},
            event=cancel,
        )


def test_visibility_is_read_once_for_libraries_in_the_ceiling_only():
    from app.services.global_citation_check import GlobalCitationCheck

    batched: list = []
    single: list = []

    class _Sources:
        def evidence_fingerprints(self, ids):
            return {}

        def visible_source_ids_by_notebook(self, ids):
            batched.append(tuple(ids))
            return {nb: [f"s-{nb}"] for nb in ids}

        def all_visible_source_ids(self, nb):  # pragma: no cover
            single.append(nb)
            return []

    citations = [Citation(label="l", source_id=f"s-{nb}", element_id="",
                          location_label="", quoted_span="q", notebook_id=nb)
                 for nb in ("a", "b", "stray")]
    outcome = GlobalCitationCheck(_Sources(), notebook_timeout_seconds=5).run(
        AskResponse(conclusion="c", citations=citations), evidence={}, siblings={},
        source_ceiling={"a": {"s-a"}, "b": {"s-b"}},
    )
    assert batched == [("a", "b")] and single == []
    assert list(outcome.reasons()) == ["out_of_ceiling"]


def test_a_failed_batched_visibility_read_is_retried_per_library():
    """Failure stays per library: only the library whose own read also fails
    has its references marked unreadable."""
    from app.services.global_citation_check import GlobalCitationCheck

    events: list = []

    class _Sources:
        def evidence_fingerprints(self, ids):
            return {}

        def visible_source_ids_by_notebook(self, ids):
            raise RuntimeError("batch failed")

        def all_visible_source_ids(self, nb):
            if nb == "b":
                raise RuntimeError("b failed")
            return [f"s-{nb}"]

    citations = [Citation(label="l", source_id=f"s-{nb}", element_id="",
                          location_label="", quoted_span="q", notebook_id=nb)
                 for nb in ("a", "b")]
    outcome = GlobalCitationCheck(
        _Sources(), notebook_timeout_seconds=5, emit=events.append,
    ).run(
        AskResponse(conclusion="c", citations=citations), evidence={}, siblings={},
        source_ceiling={"a": {"s-a"}, "b": {"s-b"}},
    )
    assert dict(outcome.verdicts) == {("b", "s-b", ""): ("unverifiable", "unreadable")}
    assert [event["read"] for event in events] == ["visible_sources", "visible_sources"]


# ---------------------------------------------------------------------------
# Robust stored-summary reads (fix round item 6c)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stored,expected", [
    ({"failed": "abc", "changed": 1}, None),
    ({"failed": [1]}, None),
    ({"failed": -1, "changed": 1}, None),
    ({"failed": True}, None),
    ({"checked": "x", "failed": 2, "changed": -5, "unverifiable": 2},
     {"outcome": "partial", "checked": 0, "failed": 2, "changed": 0,
      "source_gone": 0, "unverifiable": 2}),
    ({"outcome": "void", "checked": 3, "failed": 1, "source_gone": 1},
     {"outcome": "partial", "checked": 3, "failed": 1, "changed": 0,
      "source_gone": 1, "unverifiable": 0}),
])
def test_global_answer_check_never_raises_on_a_malformed_row(stored, expected):
    assert global_answer_check({"payload": {"answer": {"citation_check": stored}}}) == expected


def test_the_public_projection_uses_the_same_coercion():
    from app.services.conversation_public_view import _citation_check_field

    stored = {"checked": "x", "failed": 2, "changed": -5, "unverifiable": 2}
    assert _citation_check_field({"citation_check": stored}) == {
        "citation_check": global_answer_check({"answer": {"citation_check": stored}}),
    }


def test_the_notice_never_under_reports_when_counts_do_not_add_up():
    assert citation_check_notice({"failed": 3, "changed": 1}) == (
        f"本次回答有部分引用未通过核对：共 3 条。{_TAIL}"
    )


# ---------------------------------------------------------------------------
# Statement counts on the real SQLite store (items 4 and 10); PG twin in
# tests/postgres/test_global_citation_race_pg.py
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_libraries(tmp_path):
    from app.models.notebooks import NotebookCreate
    from app.repositories.sqlite.source_store import SourceStore
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'libs.db'}",
        storage_dir=str(tmp_path / "storage"),
    ))
    notebook_ids = [repo.create_notebook(NotebookCreate(name=f"lib{i}")).id for i in range(8)]
    database = repo._runtime.source_store.database
    sources = object.__new__(SourceStore)
    sources.database = database
    try:
        yield sources, database, notebook_ids
    finally:
        repo.close()


def test_sqlite_terminal_check_over_eight_libraries_issues_two_statements(sqlite_libraries):
    """Fingerprints + one batched visibility read, independent of how many
    libraries are cited (8 here)."""
    from tests.citation_check_testkit import terminal_check_statements

    sources, database, notebook_ids = sqlite_libraries
    statements, outcome = terminal_check_statements(sources, database, "?", notebook_ids)
    assert len(statements) == 2, statements
    assert outcome.checked == 8 and outcome.failed == 0


def test_sqlite_single_notebook_liveness_is_one_read_and_drops_dead_cards(sqlite_libraries):
    from app.services.reference_liveness import drop_dangling_references
    from tests.citation_check_testkit import (
        assert_dangling_dropped, count_statements, dangling_response, seed_libraries,
    )

    sources, database, notebook_ids = sqlite_libraries
    live_id = seed_libraries(database, "?", notebook_ids[:1])[notebook_ids[0]]
    response = dangling_response(live_id, f"src-{notebook_ids[0]}")
    with count_statements(database) as statements:
        drop_dangling_references(response, sources.evidence_fingerprints)
    assert len(statements) == 1
    assert_dangling_dropped(response, live_id)


def test_sqlite_live_references_are_byte_identical_after_one_read(sqlite_libraries):
    from app.services.reference_liveness import drop_dangling_references
    from tests.citation_check_testkit import count_statements, dangling_response, seed_libraries

    sources, database, notebook_ids = sqlite_libraries
    live_id = seed_libraries(database, "?", notebook_ids[:1])[notebook_ids[0]]
    response = dangling_response(live_id, f"src-{notebook_ids[0]}")
    response.citations = response.citations[:1]
    response.anchors = response.anchors[2:]
    before = response.model_dump_json()
    with count_statements(database) as statements:
        drop_dangling_references(response, sources.evidence_fingerprints)
    assert len(statements) == 1
    assert response.model_dump_json() == before


def test_liveness_without_element_references_reads_nothing_and_a_failed_read_keeps_cards():
    from app.services.reference_liveness import drop_dangling_references
    from tests.citation_check_testkit import dangling_response

    reads: list = []
    bare = AskResponse(conclusion="c", answer="a", citations=[Citation(
        label="l", source_id="s", element_id="", location_label="", quoted_span="q",
    )])
    drop_dangling_references(bare, lambda ids: reads.append(ids) or {})
    assert reads == []

    response = dangling_response("el-live", "s")
    before = response.model_dump_json()

    def broken(ids):
        raise RuntimeError("database went away")

    events: list = []
    drop_dangling_references(response, broken, emit=events.append)
    assert response.model_dump_json() == before
    # P3-6: a broken adapter is visible -- one content-free event, no ids or text
    assert events == [{
        "kind": "reference_liveness_read_failed", "surface": "answer",
        "error_type": "RuntimeError", "elements": 3,
    }]
    drop_dangling_references(response, broken)  # no sink: still fail-open
    assert response.model_dump_json() == before


def test_sqlite_report_liveness_is_one_read_per_report_and_prunes_dead_cards(sqlite_libraries):
    """Item 3: J2 for deep reports -- ONE batched read for the whole report,
    over the elements its sections actually cite, on the real store."""
    from app.services.reference_liveness import prune_dead_report_elements
    from tests.citation_check_testkit import (
        assert_report_pruned, count_statements, dangling_report_sections, seed_libraries,
    )

    sources, database, notebook_ids = sqlite_libraries
    live_id = seed_libraries(database, "?", notebook_ids[:1])[notebook_ids[0]]
    sections = dangling_report_sections(live_id, f"src-{notebook_ids[0]}")
    with count_statements(database) as statements:
        pruned = prune_dead_report_elements(sections, sources.evidence_fingerprints)
    assert len(statements) == 1, statements
    assert_report_pruned(sections, pruned, live_id)

    live_only = [sections[1]]
    with count_statements(database) as statements:
        assert prune_dead_report_elements(live_only, sources.evidence_fingerprints)[0] is live_only[0]
    assert len(statements) == 1


def test_report_liveness_reads_nothing_without_cited_elements_and_fails_open():
    from app.services.reference_liveness import prune_dead_report_elements
    from tests.citation_check_testkit import dangling_report_sections

    reads: list = []
    uncited = [{"title": "A", "markdown": "no markers", "id_map": {
        "k1": {"object_type": "element", "element_id": "el-dead"}}}]
    assert prune_dead_report_elements(uncited, lambda ids: reads.append(ids) or {}) == uncited
    assert reads == []

    sections = dangling_report_sections("el-live", "s")
    prune_dead_report_elements(sections, lambda ids: reads.append(list(ids)) or {"el-live": 1})
    assert reads == [["el-dead", "el-dead-2", "el-live"]]  # k4 is not cited: not read

    events: list = []

    def broken(ids):
        raise RuntimeError("database went away")

    kept = prune_dead_report_elements(sections, broken, emit=events.append)
    assert [a is b for a, b in zip(kept, sections)] == [True, True]
    assert events == [{
        "kind": "reference_liveness_read_failed", "surface": "report",
        "error_type": "RuntimeError", "elements": 3,
    }]


def test_liveness_propagates_cancellation():
    from app.services.cancellation import AskCancelled
    from app.services.reference_liveness import drop_dangling_references
    from tests.citation_check_testkit import dangling_response

    def cancelled(ids):
        raise AskCancelled()

    with pytest.raises(AskCancelled):
        drop_dangling_references(dangling_response("el-live", "s"), cancelled)

    from app.services.reference_liveness import prune_dead_report_elements
    from tests.citation_check_testkit import dangling_report_sections

    events: list = []
    with pytest.raises(AskCancelled):
        prune_dead_report_elements(
            dangling_report_sections("el-live", "s"), cancelled, emit=events.append,
        )
    assert events == []


@pytest.mark.parametrize("mutation", ["none", "update", "delete"])
def test_sqlite_overlay_passage_race_before_the_terminal_read(sqlite_store, mutation):
    """Closing item 16: a passage only the mix KG-overlay leg recalled is
    registered like a federated one, so a clean run passes and an edit or a
    deletion before the terminal read is reported."""
    from tests.citation_check_testkit import overlay_passage_race

    sources, database, notebook_id = sqlite_store
    overlay_passage_race(sources, database, "?", notebook_id, mutation)


def test_overlay_registration_is_a_no_op_outside_a_global_run():
    from tests.citation_check_testkit import overlay_is_a_no_op_outside_a_global_run

    overlay_is_a_no_op_outside_a_global_run(None)
