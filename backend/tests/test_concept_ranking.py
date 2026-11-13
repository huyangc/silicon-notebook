"""Ranking executor against real repositories, with bounded-I/O failure probes."""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.concept_ranking_contracts import ConceptRankWrite, PrecomputedRankRow, top_concept_ranks
from app.models.schemas import NotebookCreate
from app.repositories.read_budget import ReadBudgetExceeded
from app.services import concept_ranking as ranking_module
from app.services.cancellation import AskCancelled
from app.services.concept_ranking import (
    ConceptRankingService,
    ConceptRankRequest,
    RankCursorError,
    RankCursorStaleError,
    RankRequestError,
)
from app.services.source_scope import source_scope_context
from tests import concept_rank_store_cases as cases
from tests.kg_findings_store_cases import T0, _writer
from tests.test_kg_findings_store import repo as repo


def seed_rank_world(repo, *, postgres):
    nb = cases.seed(repo, postgres=postgres)
    # Raw fixture inserts bypass the production writer's mutation stamp.
    with repo._runtime.database.write() as db:
        repo._runtime.unified_kg.mark_dirty(db, nb, T0)
    return nb


def service(repo, **overrides):
    rt = repo._runtime
    options = dict(database=rt.database, unified_kg=rt.unified_kg, sources=rt.source_store,
                   catalog=rt.collection_catalog, enumeration=rt.collection_enumeration,
                   settings=rt.settings, producible=lambda nb: True,
                   refresh_settled=lambda nb: False,
                   notebook_names=lambda ids: {nb: "排行库" for nb in ids})
    options.update(overrides)
    return ConceptRankingService(**options)


def request(svc, nb, *, query="", retrieval=None, **kwargs):
    return svc.rank(nb, ConceptRankRequest(query=query), retrieval=retrieval,
                    ceiling_binds=True, participants=(nb,), **kwargs)


def hits(*ids):
    return SimpleNamespace(retrieve_scored=Mock(return_value=[
        SimpleNamespace(object_id=oid, object_type="concept") for oid in ids]))


def publish_current(repo, nb):
    store = repo._runtime.unified_kg
    with repo._runtime.database.connect() as db:
        entries = top_concept_ranks(store.concept_rank_full_rows(db, nb, ceiling=None))
        representatives = store.concept_rank_representatives(db, nb, [row[0] for row in entries])
        seq = store.graph_seq_row(db, nb)[:2]
    cases.publish(repo, nb, ConceptRankWrite(*seq, tuple(
        PrecomputedRankRow(i, canonical, representatives[canonical], sc, oc)
        for i, (canonical, sc, oc) in enumerate(entries, 1))))


def check_paths(repo, monkeypatch, *, postgres):
    nb = seed_rank_world(repo, postgres=postgres)
    svc = service(repo)
    full = request(svc, nb).computation.libraries[0]
    assert full.coverage.path == "scope_full" and full.coverage.exact
    assert full.coverage.scope_sources == 12
    assert repo._runtime.source_store.visible_source_count(nb) == len(
        repo._runtime.source_store.all_visible_source_ids(nb)) == 12
    assert {r.canonical_id: (r.source_count, r.object_count) for r in full.rows} == cases.EXPECTED
    assert {r.same_name_group for r in full.rows if r.name == "同名"} == {"g1"}
    publish_current(repo, nb)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    precomputed = request(svc, nb).computation.libraries[0]
    assert precomputed.coverage.path == "precomputed"
    assert precomputed.coverage.exact and not precomputed.coverage.stale_counts
    assert precomputed.rows == full.rows
    retrieval = hits("wide-00", "wide-01", "single", "claim")
    topical = request(svc, nb, query="主题", retrieval=retrieval).computation.libraries[0]
    assert topical.coverage.path == "query" and not topical.coverage.exact
    assert [(r.canonical_id, r.source_count) for r in topical.rows] == [("wide", 9), ("single", 1)]
    assert retrieval.retrieve_scored.call_args.kwargs["allowed_source_ids"] is None


def test_s_p_q_paths(repo, monkeypatch):
    check_paths(repo, monkeypatch, postgres=False)


def check_scope(repo, monkeypatch, *, postgres):
    nb = seed_rank_world(repo, postgres=postgres)
    svc = service(repo)
    scope = {"mode": "include", "source_ids": ["s01"], "narrowed": True}
    with source_scope_context(nb, scope):
        library = request(svc, nb).computation.libraries[0]
        assert library.coverage.scoped and library.coverage.exact
        assert library.coverage.scope_sources == 1
        assert {r.canonical_id: r.source_count for r in library.rows} == {
            "blank": 1, "scope": 1, "space": 1, "wide": 1, "same-a": 1}
        assert next(r for r in library.rows if r.canonical_id == "scope").name == "范围内"
        retrieval = hits("wide-01")
        topical = request(svc, nb, query="主题", retrieval=retrieval).computation.libraries[0]
        assert topical.coverage.candidates_lexical_only
        assert retrieval.retrieve_scored.call_args.kwargs["allowed_source_ids"] == ["s01"]
    with source_scope_context(nb, {**scope, "narrowed": False}):
        drifted = svc.rank(nb, ConceptRankRequest(), retrieval=None,
                           ceiling_binds=False, participants=(nb,)).computation.libraries[0]
        assert drifted.coverage.drifted and not drifted.coverage.exact
        assert drifted.coverage.scope_sources == 12
        assert next(r for r in drifted.rows if r.canonical_id == "wide").source_count == 9
        assert next(r for r in drifted.rows if r.canonical_id == "scope").name == "范围内"
        # An un-narrowed run with a bound ceiling keeps semantic candidate
        # retrieval: no explicit allow-list reaches the retrieval layer.
        retrieval = hits("wide-01")
        unnarrowed = svc.rank(nb, ConceptRankRequest(query="主题"), retrieval=retrieval,
                              ceiling_binds=True, participants=(nb,)).computation.libraries[0]
        assert not unnarrowed.coverage.scoped and not unnarrowed.coverage.candidates_lexical_only
        assert retrieval.retrieve_scored.call_args.kwargs["allowed_source_ids"] is None


def test_narrowed_and_drifted_scopes(repo, monkeypatch):
    check_scope(repo, monkeypatch, postgres=False)


@pytest.mark.parametrize("query", ["", "主题"])
def test_large_narrowed_refuses_before_count_or_retrieval(repo, monkeypatch, query):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    for name in ("concept_rank_full_rows", "concept_recount_rows"):
        monkeypatch.setattr(repo._runtime.unified_kg, name, Mock(side_effect=AssertionError(name)))
    retrieval = hits("wide-01")
    with source_scope_context(nb, {"mode": "include", "source_ids": ["s01"], "narrowed": True}):
        coverage = request(service(repo), nb, query=query, retrieval=retrieval).computation.libraries[0].coverage
    assert (coverage.status, coverage.reason, coverage.path) == ("unsupported", "scoped_large_notebook", "")
    retrieval.retrieve_scored.assert_not_called()
    repo._runtime.unified_kg.concept_rank_full_rows.assert_not_called()
    repo._runtime.unified_kg.concept_recount_rows.assert_not_called()


@pytest.mark.parametrize("scenario", ["all_selected_drift", "global_upload", "empty_frozen", "only_reference"])
def test_large_scope_does_not_confuse_freeze_with_narrowing(repo, monkeypatch, scenario):
    nb = seed_rank_world(repo, postgres=False)
    other = repo.create_notebook(NotebookCreate(name="active")).id
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    svc = service(repo)
    if scenario == "all_selected_drift":
        context = source_scope_context(nb, {"mode": "include", "source_ids": ["s01"], "narrowed": False})
        active, participants = nb, (nb,)
    elif scenario == "global_upload":
        context = source_scope_context(nb, None, notebook_source_ceilings={nb: ["s01"]}, subjectless=True)
        active, participants = nb, (nb,)
    elif scenario == "empty_frozen":
        context = source_scope_context(other, None, notebook_source_ceilings={nb: []})
        active, participants = other, (nb,)
    else:
        context = source_scope_context(other, {"mode": "include", "source_ids": [], "narrowed": True},
                                       notebook_source_ceilings={nb: ["s01"]})
        active, participants = other, (other, nb)
    with context:
        page = svc.rank(active, ConceptRankRequest(), retrieval=None, ceiling_binds=True, participants=participants)
    assert all(lib.coverage.reason != "scoped_large_notebook" for lib in page.computation.libraries)
    if scenario == "empty_frozen":
        assert page.computation.libraries == () and page.failed_libraries() == ()
    if scenario == "only_reference":
        assert [lib.coverage.notebook_id for lib in page.computation.libraries] == [nb]


@pytest.mark.parametrize("producible,settled,reason", [
    (True, False, "not_computed"), (False, False, "analysis_unavailable"),
    (True, True, "refresh_produced_nothing"),
])
def test_missing_precomputation_reasons(repo, monkeypatch, producible, settled, reason):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    svc = service(repo, producible=lambda nb: producible, refresh_settled=lambda nb: settled)
    assert request(svc, nb).failed_libraries()[0].reason == reason


def test_precomputed_staleness_canonical_drift_and_deleted_representative(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    publish_current(repo, nb)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE kg_concept_ranks SET built_at_kg_seq=-2,built_at_cluster_seq=-3 WHERE notebook_id=?", (nb,))
        db.execute("UPDATE concept_clusters SET canonical_id='merged' WHERE notebook_id=? AND canonical_id='wide'", (nb,))
        db.execute("DELETE FROM knowledge_objects WHERE id='single'")
    lib = request(service(repo), nb).computation.libraries[0]
    with repo._runtime.database.connect() as db:
        kg, cl = repo._runtime.unified_kg.graph_seq_row(db, nb)[:2]
    assert lib.coverage.stale_counts and not lib.coverage.exact
    assert lib.coverage.seq_behind == kg + cl + 5
    assert lib.rows[0].canonical_id == "merged" and lib.rows[0].source_count == 9
    assert all(row.canonical_id != "single" for row in lib.rows)
    assert lib.coverage.unnamed_dropped == 1


def test_precomputed_duplicate_current_canonical_keeps_first_rank(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    publish_current(repo, nb)
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
    store = repo._runtime.unified_kg
    original = store.concept_rank_precomputed_rows
    def duplicate(db, notebook):
        rows = original(db, notebook)
        rows[1]["current_canonical_id"] = rows[0]["canonical_id"]
        return rows
    monkeypatch.setattr(store, "concept_rank_precomputed_rows", duplicate)
    lib = request(service(repo), nb).computation.libraries[0]
    assert lib.coverage.stale_counts and lib.coverage.seq_behind == 0 and not lib.coverage.exact
    assert len(lib.rows) == len(cases.EXPECTED) - 1
    assert (lib.rows[0].source_count, lib.rows[0].object_count) == (9, 10)


@pytest.mark.parametrize("mode", ["row_cap", "timeout_second", "timeout_first", "cancel"])
def test_recount_batches_and_limits(repo, monkeypatch, mode):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(store, "concept_rank_member_counts", lambda *args: {"wide": 10001, "single": 1})
    if mode == "row_cap":
        monkeypatch.setattr(repo._runtime.settings, "kg_rank_recount_max_rows", 10001)
    original = store.concept_recount_rows
    batches = []
    cancel = threading.Event()
    def recount(db, notebook, canonical_ids, *, ceiling):
        batches.append(list(canonical_ids))
        if mode in ("timeout_first", "cancel") or mode == "timeout_second" and len(batches) == 2:
            if mode == "cancel":
                cancel.set()
            raise ReadBudgetExceeded("test")
        return original(db, notebook, canonical_ids, ceiling=ceiling)
    monkeypatch.setattr(store, "concept_recount_rows", recount)
    svc = service(repo)
    if mode == "cancel":
        with pytest.raises(AskCancelled):
            request(svc, nb, query="主题", retrieval=hits("wide-00", "single"), cancel_event=cancel)
        return
    lib = request(svc, nb, query="主题", retrieval=hits("wide-00", "single")).computation.libraries[0]
    assert batches[0] == ["wide"]
    assert lib.coverage.recount_capped
    assert lib.coverage.recount_skipped == (2 if mode == "timeout_first" else 1)
    if mode == "timeout_first":
        assert lib.coverage.reason == "recount_budget_exhausted"
    else:
        assert lib.coverage.status == "ok"
        assert [(r.canonical_id, r.source_count) for r in lib.rows] == [("wide", 9)]


def test_empty_query_candidates_are_successful(repo):
    nb = seed_rank_world(repo, postgres=False)
    lib = request(service(repo), nb, query="absent", retrieval=hits()).computation.libraries[0]
    assert lib.coverage.status == "ok" and lib.rows == ()


def test_f2_and_owner_evidence_json_shapes(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(store, "kg_analysis_artifact_rows", lambda *args: {
        "findings": {"payload": {"rules": {"F2": {"status": "ran", "pairs_hit": 7, "total": 3}}}}})
    raw = [
        ("wide", "a", "s00", '[{"source_id":"other","element_id":"wrong"},{"source_id":"s00","element_id":"e0"}]'),
        ("wide", "b", "s00", [{"source_id": "s00", "element_id": "duplicate"}]),
        ("wide", "c", "s01", [{"source_id": "s01", "element_id": "e1"}]),
        ("wide", "d", "s02", "bad JSON"),
        ("wide", "e", "s03", [{"source_id": "s03", "element_id": "fourth"}]),
    ]
    monkeypatch.setattr(store, "concept_rank_citation_rows", lambda *args, **kwargs: raw)
    lib = request(service(repo), nb).computation.libraries[0]
    assert lib.coverage.duplicate_import_pairs == 7
    # s02's unparseable evidence spends no slot, so s03 fills the third one.
    assert [e.element_id for e in lib.rows[0].evidence] == ["e0", "e1", "fourth"]
    assert all(e.notebook_id == nb for e in lib.rows[0].evidence)


def test_multiple_libraries_failure_isolation(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    failed = repo.create_notebook(NotebookCreate(name="failed")).id
    timeout = repo.create_notebook(NotebookCreate(name="timeout")).id
    original = repo._runtime.source_store.visible_source_count
    def visible(notebook):
        if notebook == failed:
            raise RuntimeError("read failed")
        if notebook == timeout:
            raise ReadBudgetExceeded("timeout")
        return original(notebook)
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count", visible)
    page = service(repo).rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=True,
                              participants=(nb, failed, timeout))
    assert [lib.coverage.notebook_id for lib in page.computation.libraries] == [nb, failed, timeout]
    assert [c.skipped for c in page.failed_libraries()] == ["read_failed", "timeout"]
    assert page.page_rows()


def seed_extra_rows(repo, nb, *, postgres=False, count=45):
    with repo._runtime.database.write() as db:
        ins = _writer(db, postgres)
        for i in range(count):
            ins("knowledge_objects", "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
                (f"extra-{i:03}", nb, "concept", "approved", json.dumps({"name": f"extra {i}"}), "[]", "s00", T0, T0),
                json_columns=("payload", "evidence"))


def test_cursor_pagination_memo_identity_and_staleness(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    svc, memo = service(repo), {}
    rank = lambda req: svc.rank(nb, req, retrieval=None, ceiling_binds=True, participants=(nb,), memo=memo)
    first = rank(ConceptRankRequest(page_size=10))
    original = repo._runtime.unified_kg.concept_rank_full_rows
    spy = Mock(wraps=original)
    monkeypatch.setattr(repo._runtime.unified_kg, "concept_rank_full_rows", spy)
    second = rank(ConceptRankRequest(cursor=first.next_cursor, page_size=10))
    third = rank(ConceptRankRequest(cursor=second.next_cursor, page_size=10))
    assert [first.page_number, second.page_number, third.page_number] == [0, 1, 2]
    combined = first.page_rows() + second.page_rows() + third.page_rows()
    assert combined == first.computation.libraries[0].rows[:30]
    assert len({r.canonical_id for r in combined}) == 30
    assert rank(ConceptRankRequest()).computation is first.computation
    spy.assert_not_called()
    with pytest.raises(RankCursorError):
        rank(ConceptRankRequest(query="changed", cursor=first.next_cursor))
    with pytest.raises(RankCursorError):
        rank(ConceptRankRequest(cursor=first.next_cursor, page_size=20))
    with repo._runtime.database.write() as db:
        repo._runtime.unified_kg.mark_dirty(db, nb, T0)
    with pytest.raises(RankCursorStaleError):
        rank(ConceptRankRequest(cursor=first.next_cursor, page_size=10))
    assert rank(ConceptRankRequest()).computation is not first.computation
    assert spy.call_count == 1
    page = rank(ConceptRankRequest())
    while page.next_cursor:
        page = rank(ConceptRankRequest(cursor=page.next_cursor))
    assert page.next_cursor == ""


def test_query_cursor_binds_canonical_set_without_binding_recall_order(repo):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    candidates = [f"extra-{i:03}" for i in range(30)]
    retrieval = hits("wide-00", "wide-01", *candidates)
    svc = service(repo)
    rank = lambda cursor="": svc.rank(
        nb, ConceptRankRequest(query="主题", cursor=cursor, page_size=10),
        retrieval=retrieval, ceiling_binds=False, participants=(nb,), bind_query_candidates=True)
    first = rank()
    # Changing aliases and duplicates preserves the mapped canonical set.
    retrieval.retrieve_scored.return_value = hits(*reversed(candidates), "wide-01").retrieve_scored.return_value
    second = rank(first.next_cursor)
    assert second.page_number == 1
    assert first.page_rows() + second.page_rows() == first.computation.libraries[0].rows[:20]
    assert first.computation.ceiling_digest == second.computation.ceiling_digest
    assert retrieval.retrieve_scored.call_count == 2
    retrieval.retrieve_scored.return_value = hits(*candidates[:-1], "extra-030", "wide-00").retrieve_scored.return_value
    with pytest.raises(RankCursorStaleError, match="query candidates changed"):
        rank(second.next_cursor)
    assert retrieval.retrieve_scored.call_count == 3


def test_query_cursor_binds_candidates_beyond_retained_eighty_rows(repo):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb, count=91)
    candidates = [f"extra-{i:03}" for i in range(90)]
    retrieval, svc = hits(*candidates), service(repo)
    first = request(svc, nb, query="主题", retrieval=retrieval, bind_query_candidates=True)
    assert len(first.computation.libraries[0].rows) == 80
    assert all(row.canonical_id != "extra-089" for row in first.computation.libraries[0].rows)
    retrieval.retrieve_scored.return_value = hits(*candidates[:-1], "extra-090").retrieve_scored.return_value
    with pytest.raises(RankCursorStaleError, match="query candidates changed"):
        svc.rank(nb, ConceptRankRequest(query="主题", cursor=first.next_cursor),
                 retrieval=retrieval, ceiling_binds=True, participants=(nb,), bind_query_candidates=True)
    assert retrieval.retrieve_scored.call_count == 2


def test_reflect_query_cursor_keeps_existing_run_memo_and_digest(repo):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    retrieval = hits(*(f"extra-{i:03}" for i in range(30)))
    svc, memo = service(repo), {}
    first = request(svc, nb, query="主题", retrieval=retrieval, memo=memo)
    retrieval.retrieve_scored.return_value = hits(*(f"extra-{i:03}" for i in range(1, 31))).retrieve_scored.return_value
    second = svc.rank(nb, ConceptRankRequest(query="主题", cursor=first.next_cursor),
                      retrieval=retrieval, ceiling_binds=True, participants=(nb,), memo=memo)
    assert second.computation is first.computation
    assert second.computation.ceiling_digest == ""
    retrieval.retrieve_scored.assert_called_once()
    recomputed = svc.rank(nb, ConceptRankRequest(query="主题", cursor=first.next_cursor),
                          retrieval=retrieval, ceiling_binds=True, participants=(nb,))
    assert recomputed.page_number == 1 and not recomputed.failed_libraries()
    assert retrieval.retrieve_scored.call_count == 2


@pytest.mark.parametrize("failure", ["retrieval", "mapping", "member_counts", "recount", "source_count"])
def test_query_cursor_returns_failures_instead_of_candidate_staleness(repo, monkeypatch, failure):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    retrieval = hits(*(f"extra-{i:03}" for i in range(30)))
    svc = service(repo)
    first = request(svc, nb, query="主题", retrieval=retrieval, bind_query_candidates=True)
    retrieval.retrieve_scored.return_value = hits(*(f"extra-{i:03}" for i in range(1, 31))).retrieve_scored.return_value
    if failure == "retrieval":
        retrieval.retrieve_scored.side_effect = RuntimeError("recall failed")
    elif failure == "source_count":
        monkeypatch.setattr(repo._runtime.source_store, "visible_source_count",
                            Mock(side_effect=ReadBudgetExceeded("source read timeout")))
    else:
        method = {"mapping": "concept_rank_candidate_canonicals", "member_counts": "concept_rank_member_counts",
                  "recount": "concept_recount_rows"}[failure]
        monkeypatch.setattr(repo._runtime.unified_kg, method, Mock(side_effect=ReadBudgetExceeded("count timeout")))
    second = svc.rank(nb, ConceptRankRequest(query="主题", cursor=first.next_cursor),
                      retrieval=retrieval, ceiling_binds=True, participants=(nb,), bind_query_candidates=True)
    assert second.page_number == 1 and second.page_rows() == () and second.next_cursor == ""
    failed = second.failed_libraries()[0]
    assert failed.status == "unavailable"
    if failure == "source_count":
        assert failed.skipped == "timeout"
    else:
        assert failed.reason == ("rank_failed" if failure == "retrieval" else "recount_budget_exhausted")


def test_query_cursor_checks_scope_before_fresh_candidate_retrieval(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    retrieval = hits(*(f"extra-{i:03}" for i in range(30)))
    svc = service(repo)
    first = request(svc, nb, query="主题", retrieval=retrieval, bind_query_candidates=True)
    monkeypatch.setattr(repo._runtime.collection_catalog, "scope_ceiling_digest", lambda *args, **kwargs: "changed")
    with pytest.raises(RankCursorStaleError, match="source ceiling changed"):
        svc.rank(nb, ConceptRankRequest(query="主题", cursor=first.next_cursor),
                 retrieval=retrieval, ceiling_binds=True, participants=(nb,), bind_query_candidates=True)
    retrieval.retrieve_scored.assert_called_once()


@pytest.mark.parametrize("changes", [{"query": "x" * 201}, {"scope": "wrong"}, {"page_size": 0}, {"page_size": 21}, {"page_size": True}])
def test_request_validation(repo, changes):
    with pytest.raises(RankRequestError):
        service(repo).rank("none", replace(ConceptRankRequest(), **changes), retrieval=None, ceiling_binds=True)


@pytest.mark.parametrize("page_number", [True, -1, 4, 80, 1.5, "1"])
def test_cursor_page_number_validation(repo, page_number):
    nb = seed_rank_world(repo, postgres=False)
    svc = service(repo)
    page = request(svc, nb)
    cursor = base64.urlsafe_b64encode(json.dumps(dict(
        v=2, f=page.computation.fingerprint, p=page_number, l=20, s=page.computation.seqs,
        d=page.computation.ceiling_digest)).encode()).decode().rstrip("=")
    with pytest.raises(RankCursorError):
        svc.rank(nb, ConceptRankRequest(cursor=cursor), retrieval=None, ceiling_binds=True, participants=(nb,))


@pytest.mark.parametrize("changes", [{"v": 1}, {"l": True}, {"l": 20.0}, {"l": 10}, {"l": "20"}])
def test_cursor_version_and_page_size_binding(repo, changes):
    nb = seed_rank_world(repo, postgres=False)
    svc = service(repo)
    page = request(svc, nb)
    payload = dict(v=2, f=page.computation.fingerprint, p=1, l=20,
                   s=page.computation.seqs, d=page.computation.ceiling_digest)
    payload.update(changes)
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(RankCursorError):
        svc.rank(nb, ConceptRankRequest(cursor=cursor), retrieval=None,
                 ceiling_binds=True, participants=(nb,))


def test_legacy_row_offset_cursor_is_rejected(repo):
    nb = seed_rank_world(repo, postgres=False)
    svc = service(repo)
    computation = request(svc, nb).computation
    payload = dict(v=1, f=computation.fingerprint, o=20,
                   s=computation.seqs, d=computation.ceiling_digest)
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(RankCursorError):
        svc.rank(nb, ConceptRankRequest(cursor=cursor), retrieval=None,
                 ceiling_binds=True, participants=(nb,))


def seed_paging_libraries(repo, counts):
    notebooks = []
    for count in counts:
        nb = repo.create_notebook(NotebookCreate(name=f"library-{len(notebooks)}")).id
        notebooks.append(nb)
        with repo._runtime.database.write() as db:
            insert = _writer(db, False)
            source_id = f"{nb}-source"
            insert("sources", "id,notebook_id,title,source_type,status,created_at,updated_at",
                   (source_id, nb, "source", "markdown", "extracted", T0, T0))
            for index in range(count):
                insert("knowledge_objects",
                       "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
                       (f"{nb}-{index:03}", nb, "concept", "approved",
                        json.dumps({"name": f"concept-{index}"}), "[]", source_id, T0, T0),
                       json_columns=("payload", "evidence"))
            repo._runtime.unified_kg.mark_dirty(db, nb, T0)
    return tuple(notebooks)


@pytest.mark.parametrize("counts,expected", [([10, 10, 10], [7, 7, 6]), ([1] * 25, [1] * 25)])
def test_multilibrary_page_quotas(repo, counts, expected):
    notebooks = seed_paging_libraries(repo, counts)
    page = service(repo).rank(notebooks[0], ConceptRankRequest(), retrieval=None,
                             ceiling_binds=True, participants=notebooks)
    rows = page.page_rows()
    assert [sum(row.notebook_id == nb for row in rows) for nb in notebooks] == expected
    assert len(rows) == sum(expected)
    assert not page.failed_libraries()
    if len(notebooks) > 20:
        assert page.next_cursor == ""


def test_short_library_exhaustion_does_not_stop_long_library_at_eighty(repo):
    notebooks = seed_paging_libraries(repo, [2, 80, 8])
    svc, memo = service(repo), {}
    pages, cursor = [], ""
    while True:
        page = svc.rank(notebooks[0], ConceptRankRequest(cursor=cursor), retrieval=None,
                        ceiling_binds=True, participants=notebooks, memo=memo)
        pages.append(page)
        cursor = page.next_cursor
        if not cursor:
            break
        assert len(pages) < 20
    assert [page.page_number for page in pages] == list(range(12))
    assert [sum(row.notebook_id == nb for row in pages[0].page_rows()) for nb in notebooks] == [2, 7, 6]
    assert [sum(row.notebook_id == nb for row in pages[1].page_rows()) for nb in notebooks] == [0, 7, 2]
    assert [sum(row.notebook_id == nb for row in pages[-1].page_rows()) for nb in notebooks] == [0, 3, 0]
    for nb, count in zip(notebooks, [2, 80, 8]):
        rows = [row for page in pages for row in page.page_rows() if row.notebook_id == nb]
        assert len(rows) == len({row.canonical_id for row in rows}) == count


def test_participant_resolution_and_subjectless_current_scope(repo):
    nb = seed_rank_world(repo, postgres=False)
    other = repo.create_notebook(NotebookCreate(name="other")).id
    enumeration = SimpleNamespace(rank_participants=Mock(return_value=((nb, other), {nb: "personal", other: "base"})))
    svc = service(repo, enumeration=enumeration)
    page = svc.rank(nb, ConceptRankRequest(scope="current_notebook"), retrieval=None, ceiling_binds=True)
    assert [lib.coverage.notebook_id for lib in page.computation.libraries] == [nb]
    enumeration.rank_participants.assert_called_once_with(nb)
    with source_scope_context(nb, None, notebook_source_ceilings={nb: ["s01"], other: ["other-source"]}, subjectless=True):
        page = svc.rank(nb, ConceptRankRequest(scope="current_notebook"), retrieval=None, ceiling_binds=True)
    assert [lib.coverage.notebook_id for lib in page.computation.libraries] == [nb, other]
    assert all(not lib.coverage.scoped and not lib.coverage.drifted for lib in page.computation.libraries)


@pytest.mark.parametrize("subjectless", [False, True])
def test_frozen_all_sources_then_upload_does_not_narrow_counts(repo, monkeypatch, subjectless):
    nb = seed_rank_world(repo, postgres=False)
    frozen = [f"s{i:02}" for i in range(12)]
    scope = None if subjectless else {"mode": "include", "source_ids": frozen, "narrowed": False}
    ceilings = {nb: frozen} if subjectless else None
    with source_scope_context(nb, scope, notebook_source_ceilings=ceilings, subjectless=subjectless):
        with repo._runtime.database.write() as db:
            ins = _writer(db, False)
            ins("sources", "id,notebook_id,title,source_type,status,created_at,updated_at",
                ("new-source", nb, "new", "markdown", "extracted", T0, T0))
            ins("knowledge_objects", "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
                ("new-concept", nb, "concept", "approved", '{"name":"新名字"}', "[]", "new-source", T0, T0))
            repo._runtime.unified_kg.mark_dirty(db, nb, T0)
        lib = service(repo).rank(nb, ConceptRankRequest(), retrieval=None,
                                 ceiling_binds=False, participants=(nb,)).computation.libraries[0]
        assert lib.coverage.scope_sources == 13 and not lib.coverage.scoped
        assert lib.coverage.drifted is (not subjectless)
        assert lib.coverage.unnamed_dropped == 1 and not lib.coverage.exact
        assert "新名字" not in [r.name for r in lib.rows]
        monkeypatch.setattr(repo._runtime.settings, "kg_rank_online_max_objects", 1)
        large = request(service(repo), nb).computation.libraries[0]
        assert large.coverage.reason == "not_computed"


def test_cursor_ceiling_changed_is_stale_even_when_memo_hits(repo):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    svc, memo = service(repo), {}
    with source_scope_context(nb, {"mode": "include", "source_ids": ["s00", "s01"], "narrowed": True}):
        first = request(svc, nb, memo=memo)
    assert first.next_cursor
    with source_scope_context(nb, {"mode": "include", "source_ids": ["s00"], "narrowed": True}):
        with pytest.raises(RankCursorStaleError):
            svc.rank(nb, ConceptRankRequest(cursor=first.next_cursor), retrieval=None,
                     ceiling_binds=True, participants=(nb,), memo=memo)
        restarted = request(svc, nb, memo=memo)
    assert restarted.computation is not first.computation
    assert restarted.computation.ceiling_digest != first.computation.ceiling_digest


def test_global_call_deadline_skips_unstarted_libraries(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    other = repo.create_notebook(NotebookCreate(name="second")).id
    now = [100.0]
    monkeypatch.setattr(ranking_module.time, "monotonic", lambda: now[0])
    def count(*args, **kwargs):
        now[0] += 4
        raise ReadBudgetExceeded("count deadline")
    monkeypatch.setattr(repo._runtime.unified_kg, "concept_rank_full_rows", count)
    page = service(repo).rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=True,
                              participants=(nb, other))
    assert [(c.reason, c.skipped) for c in page.failed_libraries()] == [
        ("recount_budget_exhausted", ""), ("", "timeout")]


def test_federated_plan_tightens_each_library_deadline(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(ranking_module, "federated_ask_active", lambda: True)
    monkeypatch.setattr(ranking_module, "current_federated_run_plan", lambda: SimpleNamespace(notebook_timeout_seconds=0))
    assert request(service(repo), nb).failed_libraries()[0].skipped == "timeout"


def test_pending_f2_does_not_disclose_old_pair_count(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.unified_kg, "kg_analysis_artifact_rows", lambda *args: {
        "findings": {"payload": {"rules": {"F2": {"status": "pending", "pairs_hit": 7}}}}})
    assert request(service(repo), nb).computation.libraries[0].coverage.duplicate_import_pairs == 0


@pytest.mark.parametrize("cursor", ["not-base64", "!", "e30", "W10"])
def test_malformed_cursor_is_rejected(repo, cursor):
    with pytest.raises(RankCursorError):
        service(repo).rank("none", ConceptRankRequest(cursor=cursor), retrieval=None,
                          ceiling_binds=True, participants=())


def test_cancellation_wins_during_cursor_identity_read(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    cancel = threading.Event()
    def interrupted(*args):
        cancel.set()
        raise ReadBudgetExceeded("cancelled metadata read")
    monkeypatch.setattr(repo._runtime.unified_kg, "graph_seq_row", interrupted)
    with pytest.raises(AskCancelled):
        request(service(repo), nb, cancel_event=cancel)


@pytest.mark.parametrize("cancelled", [False, True])
def test_real_sqlite_recount_timeout_preserves_completed_batch(repo, monkeypatch, cancelled):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    original = store.concept_recount_rows
    monkeypatch.setattr(store, "concept_rank_member_counts", lambda *args: {"wide": 10001, "single": 1})
    now = [100.0]
    monkeypatch.setattr(ranking_module.time, "monotonic", lambda: now[0])
    calls = []
    interrupted = []
    connections = []
    cancel = threading.Event()
    def recount(db, notebook, canonical_ids, *, ceiling):
        calls.append(tuple(canonical_ids))
        if len(calls) == 1:
            return original(db, notebook, canonical_ids, ceiling=ceiling)
        connections.append(db.connection)
        def expire():
            now[0] += 2
            if cancelled:
                cancel.set()
            return 1
        db.create_function("rank_expire", 0, expire)
        try:
            db.execute("WITH RECURSIVE work(x) AS (SELECT rank_expire() UNION ALL "
                       "SELECT x+1 FROM work WHERE x<1000000) SELECT sum(x) FROM work").fetchone()
        except sqlite3.OperationalError as exc:
            interrupted.append(str(exc))
            raise
        pytest.fail("the real SQLite budget must interrupt the statement")
    monkeypatch.setattr(store, "concept_recount_rows", recount)
    if cancelled:
        with pytest.raises(AskCancelled):
            request(service(repo), nb, query="主题", retrieval=hits("wide-00", "single"), cancel_event=cancel)
        assert interrupted == ["interrupted"]
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        return
    library = request(service(repo), nb, query="主题", retrieval=hits("wide-00", "single")).computation.libraries[0]
    assert interrupted == ["interrupted"]
    assert library.coverage.status == "ok"
    assert not library.coverage.skipped
    assert library.coverage.recount_capped and library.coverage.recount_skipped == 1
    assert [(row.canonical_id, row.source_count) for row in library.rows] == [("wide", 9)]
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


def test_deterministic_recount_row_cap_can_reuse_successful_library(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(store, "concept_rank_member_counts", lambda *args: {"wide": 10001, "single": 1})
    monkeypatch.setattr(repo._runtime.settings, "kg_rank_recount_max_rows", 10001)
    svc, memo, retrieval = service(repo), {}, hits("wide-00", "single")
    first = request(svc, nb, query="主题", retrieval=retrieval, memo=memo)
    second = request(svc, nb, query="主题", retrieval=retrieval, memo=memo)
    assert first.computation is second.computation
    assert first.computation.libraries[0].coverage.recount_capped
    retrieval.retrieve_scored.assert_called_once()


def test_mount_tiers_are_resolved_with_participants(repo):
    base = seed_rank_world(repo, postgres=False)
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM sources WHERE notebook_id=? AND source_type='memory'", (base,))
    active = repo.create_notebook(NotebookCreate(name="active")).id
    repo.mark_notebook_base(base)
    repo.replace_notebook_bases(active, [base], "user-local")
    svc = service(repo)
    page = svc.rank(active, ConceptRankRequest(), retrieval=None, ceiling_binds=True)
    assert {row.tier for row in page.page_rows() if row.notebook_id == base} == {"base"}
    assert {row.tier for row in request(svc, base).page_rows()} == {"personal"}


@pytest.mark.parametrize("reason,expected", [("timeout", "timeout"), ("unavailable", "read_failed")])
def test_empty_frozen_failed_mount_is_disclosed_but_unchecked_mount_is_not(repo, reason, expected):
    nb = seed_rank_world(repo, postgres=False)
    active = repo.create_notebook(NotebookCreate(name="active")).id
    svc = service(repo)
    with source_scope_context(active, None, notebook_source_ceilings={nb: []}, _skipped_libraries={nb: reason}):
        result = svc.rank(active, ConceptRankRequest(), retrieval=None, ceiling_binds=True, participants=(nb,))
    assert result.failed_libraries()[0].skipped == expected
    with source_scope_context(active, None, {"mode": "exclude", "notebook_ids": [nb]},
                              notebook_source_ceilings={nb: []}, _skipped_libraries={nb: reason}):
        result = svc.rank(active, ConceptRankRequest(), retrieval=None, ceiling_binds=True, participants=(nb,))
    assert not result.computation.libraries and not result.failed_libraries()


@pytest.mark.parametrize("ceiling_binds", [False, True])
def test_confirm_fast_path_detects_upload_after_frozen_all_selected(repo, ceiling_binds):
    nb = seed_rank_world(repo, postgres=False)
    frozen = repo._runtime.source_store.all_visible_source_ids(nb)
    with source_scope_context(nb, {"mode": "include", "source_ids": frozen,
                                    "hidden_source_ids": ["knowhow"], "narrowed": False}):
        with repo._runtime.database.write() as db:
            _writer(db, False)("sources", "id,notebook_id,title,source_type,status,created_at,updated_at",
                               ("after-freeze", nb, "new", "markdown", "extracted", T0, T0))
        library = service(repo).rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=ceiling_binds,
                                     participants=(nb,)).computation.libraries[0]
    assert library.coverage.drifted and not library.coverage.exact
    assert library.coverage.scope_sources == 13


def test_unscoped_rank_reads_only_source_count_and_batches_names_before_budget(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.source_store, "all_visible_source_ids", Mock(side_effect=AssertionError("materialized ids")))
    now = [100.0]
    monkeypatch.setattr(ranking_module.time, "monotonic", lambda: now[0])
    def names(ids):
        assert tuple(ids) == (nb,)
        now[0] += 60
        return {nb: "批量名字"}
    lib = request(service(repo, notebook_names=names), nb).computation.libraries[0]
    assert lib.coverage.status == "ok" and lib.coverage.notebook_name == "批量名字"
    assert lib.coverage.scope_sources == 12


def test_transient_library_failure_retries_only_that_library(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    other = repo.create_notebook(NotebookCreate(name="other")).id
    store = repo._runtime.unified_kg
    original = store.concept_rank_full_rows
    calls = {nb: 0, other: 0}
    def full(db, notebook, *, ceiling):
        calls[notebook] += 1
        if notebook == other and calls[notebook] == 1:
            raise RuntimeError("transient")
        return original(db, notebook, ceiling=ceiling)
    monkeypatch.setattr(store, "concept_rank_full_rows", full)
    names = Mock(side_effect=lambda ids: {notebook: "名字" for notebook in ids})
    svc, memo = service(repo, notebook_names=names), {}
    rank = lambda: svc.rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=True,
                            participants=(nb, other), memo=memo)
    first = rank()
    assert first.failed_libraries()[0].reason == "rank_failed"
    assert first.computation.fingerprint not in memo
    second = rank()
    assert not second.failed_libraries()
    assert calls == {nb: 1, other: 2}
    assert second.computation.libraries[0] is first.computation.libraries[0]
    assert [call.args[0] for call in names.call_args_list] == [(nb, other), (other,)]


@pytest.mark.parametrize("first_batch", [True, False])
def test_recount_time_failure_is_not_memoized(repo, monkeypatch, first_batch):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    monkeypatch.setattr(store, "concept_rank_member_counts", lambda *args: {"wide": 10001, "single": 1})
    original = store.concept_recount_rows
    calls = []
    def count(db, notebook, canonicals, *, ceiling):
        calls.append(tuple(canonicals))
        if len(calls) == (1 if first_batch else 2):
            raise ReadBudgetExceeded("transient")
        return original(db, notebook, canonicals, ceiling=ceiling)
    monkeypatch.setattr(store, "concept_recount_rows", count)
    svc, memo, retrieval = service(repo), {}, hits("wide-00", "single")
    first = request(svc, nb, query="主题", retrieval=retrieval, memo=memo)
    assert first.computation.fingerprint not in memo
    second = request(svc, nb, query="主题", retrieval=retrieval, memo=memo)
    assert not second.failed_libraries() and len(second.page_rows()) == 2
    assert not second.computation.libraries[0].coverage.recount_capped
    assert retrieval.retrieve_scored.call_count == 2


@pytest.mark.parametrize("reader", ["concept_rank_citation_rows", "kg_analysis_artifact_rows"])
def test_finish_timeout_preserves_named_count_rows_and_is_not_memoized(repo, monkeypatch, reader):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.unified_kg, reader, Mock(side_effect=ReadBudgetExceeded("finish")))
    memo = {}
    page = request(service(repo), nb, memo=memo)
    assert len(page.page_rows()) == len(cases.EXPECTED)
    assert not page.failed_libraries()
    assert page.computation.fingerprint not in memo


def test_name_timeout_keeps_prior_named_batches_and_counts_missing_names(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    store = repo._runtime.unified_kg
    original = store.concept_rank_names
    calls = []
    def names(db, notebook, ids, **kwargs):
        calls.append(tuple(ids))
        if len(calls) > 1:
            raise ReadBudgetExceeded("name budget")
        return original(db, notebook, ids, **kwargs)
    monkeypatch.setattr(store, "concept_rank_names", names)
    page = request(service(repo), nb)
    lib = page.computation.libraries[0]
    assert len(lib.rows) == 20
    assert lib.coverage.unnamed_dropped == 33 and not lib.coverage.exact
    assert not page.failed_libraries()


def test_other_members_memory_binding_is_not_source_drift(tmp_path, monkeypatch):
    from app.api.ask_routes import _validate_source_scope
    from app.models.source_scope import SourceScope
    from app.services.concept_ranking_answer import ranking_notice
    from app.services.source_scope import current_source_scope, default_ceiling_context, run_ceiling_binds
    from app.services.sqlite_repository import reset_request_user, set_request_user
    from tests.test_source_scope import _shared_notebook_with_two_members

    shared, nb, alice, _bob = _shared_notebook_with_two_members(tmp_path, monkeypatch)
    token = set_request_user(alice)
    try:
        resolved = _validate_source_scope(shared, shared.get_notebook(nb), SourceScope(mode="exclude", source_ids=[]))
        with shared._runtime.database.write() as db:
            insert = _writer(db, False)
            for source in ("src-visible", "src-memory-alice", "src-memory-bob"):
                insert("knowledge_objects",
                       "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
                       (f"concept-{source}", nb, "concept", "approved", '{"name":"概念"}', "[]", source, T0, T0),
                       json_columns=("payload", "evidence"))
            shared._runtime.unified_kg.mark_dirty(db, nb, T0)
        original = shared._runtime.unified_kg.concept_rank_full_rows
        counted = Mock(wraps=original)
        monkeypatch.setattr(shared._runtime.unified_kg, "concept_rank_full_rows", counted)
        readers = shared._runtime.ceiling_readers()
        assert readers.verdict_probes.foreign_hidden(nb, alice.id)
        with default_ceiling_context(nb, alice.id, readers, local_scope=resolved):
            assert current_source_scope().narrowed is False
            assert run_ceiling_binds(current_source_scope(), nb)
            page = request(service(shared), nb)
        library = page.computation.libraries[0]
        assert not library.coverage.drifted and library.coverage.exact
        assert [row.canonical_id for row in library.rows] == ["concept-src-visible"]
        assert counted.call_args.kwargs["ceiling"] == frozenset({"src-visible", "src-memory-alice", "src-knowhow"})
        ranked = SimpleNamespace(fingerprint=page.computation.fingerprint,
                                 computation=page.computation, last_page_rows=page.page_rows(),
                                 successful_pages=(page,))
        notice, failures = ranking_notice([ranked], cited_keys=frozenset({"k5001"}),
                                          key_owner={"k5001": (nb, "concept-src-visible")}, query_candidates=100)
        assert not failures and "统计期间来源有变化" not in notice
    finally:
        reset_request_user(token)
        shared.close()


def test_quick_count_keeps_remaining_recount_budget_for_names(repo, monkeypatch):
    from app.repositories.read_budget import current_read_budget

    nb = seed_rank_world(repo, postgres=False)
    now = [100.0]
    monkeypatch.setattr(ranking_module.time, "monotonic", lambda: now[0])
    store = repo._runtime.unified_kg
    original = store.concept_rank_names
    observed = []
    def names(*args, **kwargs):
        observed.append(current_read_budget().deadline)
        now[0] += 0.6
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "concept_rank_names", names)
    page = request(service(repo), nb)
    assert observed == [101.5]
    assert len(page.page_rows()) == len(cases.EXPECTED)
    assert page.computation.libraries[0].coverage.exact


@pytest.mark.parametrize("following", [False, True])
def test_first_oversized_canonical_is_counted_and_disclosed(repo, monkeypatch, following):
    nb = seed_rank_world(repo, postgres=False)
    monkeypatch.setattr(repo._runtime.unified_kg, "concept_rank_member_counts",
                        lambda *args: {"wide": 200001})
    library = request(service(repo), nb, query="主题",
                      retrieval=hits("wide-00", *(["single"] if following else []))).computation.libraries[0]
    assert library.coverage.status == "ok" and not library.coverage.reason
    assert library.coverage.recount_capped and library.coverage.recount_skipped == int(following)
    assert [(row.canonical_id, row.source_count) for row in library.rows] == [("wide", 9)]


def test_memo_hit_does_not_reread_notebook_names(repo):
    nb = seed_rank_world(repo, postgres=False)
    names = Mock(return_value={nb: "名字"})
    svc, memo = service(repo, notebook_names=names), {}
    first = request(svc, nb, memo=memo)
    second = request(svc, nb, memo=memo)
    assert first.computation is second.computation
    names.assert_called_once_with((nb,))


def test_failed_mounted_library_does_not_take_page_quota(repo, monkeypatch):
    active, failed = seed_paging_libraries(repo, [30, 2])
    original = repo._runtime.source_store.visible_source_count
    def count(nb):
        if nb == failed:
            raise RuntimeError("mounted read failed")
        return original(nb)
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count", count)
    page = service(repo).rank(active, ConceptRankRequest(), retrieval=None,
                             ceiling_binds=True, participants=(active, failed))
    assert len(page.page_rows()) == 20
    assert all(row.notebook_id == active for row in page.page_rows())
    assert page.failed_libraries()[0].skipped == "read_failed"


def test_successful_empty_library_keeps_its_page_quota(repo):
    notebooks = seed_paging_libraries(repo, [30, 0])
    page = service(repo).rank(notebooks[0], ConceptRankRequest(), retrieval=None,
                             ceiling_binds=True, participants=notebooks)
    assert len(page.page_rows()) == 10 and not page.failed_libraries()


def test_valid_next_page_returns_read_failure_when_no_library_succeeds(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    seed_extra_rows(repo, nb)
    svc = service(repo)
    first = request(svc, nb)
    assert first.next_cursor and len(first.page_rows()) == 20
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count",
                        Mock(side_effect=ReadBudgetExceeded("second-page read timeout")))
    second = svc.rank(nb, ConceptRankRequest(cursor=first.next_cursor), retrieval=None,
                      ceiling_binds=True, participants=(nb,))
    assert second.page_number == 1
    assert second.page_rows() == () and second.next_cursor == ""
    assert second.failed_libraries()[0].skipped == "timeout"


@pytest.mark.parametrize("remaining_successes", [0, 1])
def test_later_multilibrary_page_retains_failures_when_successful_set_shrinks(repo, monkeypatch, remaining_successes):
    notebooks = seed_paging_libraries(repo, [80, 80, 80])
    svc, memo, cursor = service(repo), {}, ""
    for page_number in range(10):
        page = svc.rank(notebooks[0], ConceptRankRequest(cursor=cursor), retrieval=None,
                        ceiling_binds=True, participants=notebooks, memo=memo)
        assert page.page_number == page_number
        cursor = page.next_cursor
    assert cursor
    original = repo._runtime.source_store.visible_source_count
    def count(nb):
        if nb in notebooks[remaining_successes:]:
            raise ReadBudgetExceeded("later-page read timeout")
        return original(nb)
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count", count)
    failed_page = svc.rank(notebooks[0], ConceptRankRequest(cursor=cursor), retrieval=None,
                           ceiling_binds=True, participants=notebooks)
    assert failed_page.page_number == 10
    assert len(failed_page.failed_libraries()) == 3 - remaining_successes
    assert all(coverage.skipped == "timeout" for coverage in failed_page.failed_libraries())
    assert failed_page.page_rows() == () and failed_page.next_cursor == ""


def test_bound_undrifted_scope_source_count_uses_frozen_visible_sources(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    frozen = [f"s{i:02}" for i in range(12)]
    scope = {"mode": "include", "source_ids": frozen, "hidden_source_ids": ["memory", "knowhow"], "narrowed": False}
    counted = Mock(wraps=repo._runtime.source_store.visible_source_count)
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count", counted)
    with source_scope_context(nb, scope):
        with repo._runtime.database.write() as db:
            db.execute("DELETE FROM sources WHERE notebook_id=? AND id='s11'", (nb,))
        monkeypatch.setattr(repo._runtime.source_store, "all_visible_source_ids",
                            Mock(side_effect=AssertionError("count must not materialize visible ids")))
        library = request(service(repo), nb).computation.libraries[0]
    assert library.coverage.scope_sources == 11
    assert not library.coverage.drifted and library.coverage.exact
    counted.assert_called_once_with(nb, ceiling=frozenset([*frozen, "memory", "knowhow"]))


@pytest.mark.parametrize("partial_cache", [False, True])
def test_new_drift_invalidates_bound_exact_memo_without_sequence_or_digest_change(repo, monkeypatch, partial_cache):
    nb = seed_rank_world(repo, postgres=False)
    other = repo.create_notebook(NotebookCreate(name="failed mount")).id
    participants = (nb, other) if partial_cache else (nb,)
    source_count = repo._runtime.source_store.visible_source_count
    def count(notebook, **kwargs):
        if notebook == other:
            raise ReadBudgetExceeded("mounted timeout")
        return source_count(notebook, **kwargs)
    monkeypatch.setattr(repo._runtime.source_store, "visible_source_count", count)
    store = repo._runtime.unified_kg
    full_rows = Mock(wraps=store.concept_rank_full_rows)
    monkeypatch.setattr(store, "concept_rank_full_rows", full_rows)
    svc, memo = service(repo), {}
    frozen = [f"s{i:02}" for i in range(12)]
    with source_scope_context(nb, {"mode": "include", "source_ids": frozen,
                                    "hidden_source_ids": ["knowhow"], "narrowed": False}):
        first = svc.rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=True,
                         participants=participants, memo=memo)
        assert first.computation.libraries[0].coverage.exact
        with repo._runtime.database.write() as db:
            _writer(db, False)("sources", "id,notebook_id,title,source_type,status,created_at,updated_at",
                               ("after-cached-freeze", nb, "new", "markdown", "extracted", T0, T0))
        second = svc.rank(nb, ConceptRankRequest(), retrieval=None, ceiling_binds=True,
                          participants=participants, memo=memo)
    assert second.computation.seqs == first.computation.seqs
    assert second.computation.ceiling_digest == first.computation.ceiling_digest
    coverage = second.computation.libraries[0].coverage
    assert coverage.drifted and not coverage.exact and coverage.scope_sources == 13
    assert full_rows.call_count == 2


def test_citation_slot_is_spent_only_on_usable_evidence(repo, monkeypatch):
    nb = seed_rank_world(repo, postgres=False)
    store = repo._runtime.unified_kg
    real = store.concept_rank_citation_rows

    def citation_rows(db, notebook_id, ids, *, ceiling, scan):
        rows = [row for row in real(db, notebook_id, ids, ceiling=ceiling, scan=scan)
                if row[0] != "wide"]
        # The first scanned member of s01 carries no usable evidence; the second does.
        rows += [("wide", "wide-x1", "s01", "[]"),
                 ("wide", "wide-x2", "s01", '[{"source_id": "s01", "element_id": "el-good"}]')]
        return rows

    monkeypatch.setattr(store, "concept_rank_citation_rows", citation_rows)
    library = request(service(repo), nb).computation.libraries[0]
    wide = next(r for r in library.rows if r.canonical_id == "wide")
    assert [e.element_id for e in wide.evidence] == ["el-good"]
