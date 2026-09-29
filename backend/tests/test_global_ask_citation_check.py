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
