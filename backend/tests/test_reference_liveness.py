"""J2, single-notebook half: a reasoning answer's cards for elements already
gone are dropped at the commit boundary with ONE batched by-id read.

The pure rule and its statement count are pinned in
``test_global_ask_citation_check.py`` (SQLite) and
``tests/postgres/test_global_citation_race_pg.py`` (PostgreSQL); this file pins
the wiring: the real engine calls it for a single-notebook reasoning answer,
and a global run (plan installed) never does.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.ask import AskRequest
from app.services import reference_liveness
from app.services.ask_service import AskService
from app.services.federated_run import FederatedRunPlan, federated_run_plan


def _service(reads):
    service = object.__new__(AskService)
    service.evidence_context = SimpleNamespace(sources=SimpleNamespace(
        evidence_fingerprints=lambda ids: reads.append(tuple(ids)) or {},
    ))
    return service


def _response():
    from tests.citation_check_testkit import dangling_response

    return dangling_response("el-live", "s")


def test_a_single_notebook_answer_is_checked_once():
    reads: list = []
    response = _response()
    _service(reads)._drop_dangling_references(response)
    assert len(reads) == 1
    assert response.citations == []


def test_a_global_run_never_takes_the_single_notebook_read():
    """In a global run an element deleted during the answer must reach the
    terminal check as source_gone, not vanish here."""
    reads: list = []
    response = _response()
    before = response.model_dump_json()
    plan = FederatedRunPlan(
        phase_timeout_seconds=1.0, notebook_timeout_seconds=1.0, executor=None,
        window=lambda: 1, cancel=None, on_library=lambda *_: None,
        on_evidence=lambda *_: None,
    )
    with federated_run_plan(plan):
        _service(reads)._drop_dangling_references(response)
    assert reads == []
    assert response.model_dump_json() == before


def test_end_to_end_single_notebook_reasoning_drops_a_dead_locator(tmp_path, monkeypatch):
    """Real repository, real engine, one notebook: the cited element is removed
    just before the liveness pass. The references naming it lose their card
    (citations) or their locator (chunk anchors keep their card); the answer
    text is intact and exactly one extra read is spent."""
    from tests.test_global_ask_engine_parity import _e2e_repo

    repo, notebooks, user_id = _e2e_repo(tmp_path, monkeypatch, answer="低温性能如下。")
    try:
        service = repo._runtime.ask_service()
        database = repo._runtime.source_store.database
        original = reference_liveness.drop_dangling_references
        seen: dict = {}

        def remove_then_check(response, read):
            victim = next(ref.element_id for ref in response.anchors if ref.element_id)
            seen["victim"] = victim
            with database.write() as db:
                db.execute("DELETE FROM source_elements WHERE id=?", (victim,))
            calls: list = []

            def counted(ids):
                calls.append(tuple(ids))
                return read(ids)

            original(response, counted)
            seen["reads"] = len(calls)

        monkeypatch.setattr(reference_liveness, "drop_dangling_references", remove_then_check)
        response = service.ask(
            notebooks[0].id, AskRequest(question="低温性能如何", mode="reasoning"),
            user_id=user_id,
        )

        assert seen["reads"] == 1
        assert response.answer.startswith("低温性能如下。")
        victim = seen["victim"]
        assert all(row.element_id != victim for row in response.citations)
        assert all(row.element_id != victim for row in response.anchors)
        assert response.citation_check is None
    finally:
        repo.close()


@pytest.mark.parametrize("marker,text,expected", [
    ("k1", "甲 [k1] 乙", "甲 乙"),
    ("k1", "甲[k1]。", "甲。"),
    ("k2", "甲 [k1, k2] 乙", "甲 [k1] 乙"),
    ("k2", "甲 【k2，k3】 乙", "甲 [k3] 乙"),
    ("k9", "甲 [k1]  乙", "甲 [k1]  乙"),
])
def test_marker_stripping_touches_only_the_dropped_keys(marker, text, expected):
    assert reference_liveness.strip_marker_keys(text, {marker}) == expected


@pytest.mark.parametrize("mix", [False, True], ids=["plain-chunk", "mix-kg-overlay"])
def test_end_to_end_single_notebook_chunk_answer_drops_a_dead_locator(tmp_path, monkeypatch, mix):
    """Closing item 21: chunk-mode answers take the same pass at
    ``_save_answer`` -- the plain branch and the mix branch, whose KG anchors
    name evidence elements. The element behind the first cited reference is
    deleted just before the pass: no reference names it afterwards, the text is
    intact, one read is spent."""
    from tests.global_citation_e2e_kit import CitingAnswerer, evidence
    from tests.model_testkit import bind_chat_client
    from tests.model_testkit import bind_rerank_client
    from tests.test_global_ask_engine_parity import _e2e_repo

    class _Rerank:
        configured = True

        def rerank(self, query, documents, on_error=None):
            return list(range(len(documents)))

    repo, notebooks, user_id = _e2e_repo(tmp_path, monkeypatch, answer="低温性能如下。")
    try:
        nb = notebooks[0]
        database = repo._runtime.source_store.database
        if mix:
            repo.settings.chunk_kg_overlay_enabled = True
            bind_rerank_client(repo, _Rerank())
            with database.connect() as db:
                row = db.execute(
                    "SELECT e.id, e.source_id FROM source_elements e JOIN sources s "
                    "ON s.id=e.source_id WHERE s.notebook_id=? AND e.text LIKE '%零下四十度%'",
                    (nb.id,),
                ).fetchone()
            repo.store_kg(nb.id, row["source_id"], [{
                "local_id": "a", "object_type": "concept",
                "payload": {"name": "低温性能", "definition": "低温下增益上升"},
                "evidence": [evidence(row["source_id"], row["id"], "零下四十度")],
            }], [])
            bind_chat_client(repo, "ask_answer", CitingAnswerer("低温性能如下。"))
        original = reference_liveness.drop_dangling_references
        seen: dict = {}

        def remove_then_check(response, read):
            victim = next(
                (a.element_id for a in response.anchors if a.object_type == "concept"),
                None,
            ) or next(ref.element_id for ref in [*response.anchors, *response.citations]
                      if ref.element_id)
            seen["victim"] = victim
            seen["kinds"] = {a.object_type for a in response.anchors}
            with database.write() as db:
                db.execute("DELETE FROM source_elements WHERE id=?", (victim,))
            calls: list = []
            original(response, lambda ids: calls.append(tuple(ids)) or read(ids))
            seen["reads"] = len(calls)

        monkeypatch.setattr(reference_liveness, "drop_dangling_references", remove_then_check)
        response = repo._runtime.ask_service().ask(
            nb.id, AskRequest(question="低温性能如何", mode="chunk"), user_id=user_id,
        )
        assert seen["reads"] == 1
        if mix:
            assert "concept" in seen["kinds"], seen["kinds"]
        assert response.answer.startswith("低温性能如下。")
        victim = seen["victim"]
        assert all(ref.element_id != victim for ref in [*response.anchors, *response.citations])
        assert response.citation_check is None
    finally:
        repo.close()
