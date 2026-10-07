"""E1-2 push-down: an unconstrained run binds no source list, and every
producer that reads without it verifies what it read.

All scopes here are the PRODUCTION default ceiling -- ``default_ceiling_context``
over ``RepositoryRuntime.ceiling_readers()``, verdict probes included -- on a
real SQLite store.  (A bare ``source_scope_context`` carries no probes, so its
verdict always binds and would make every push-down assertion vacuous.)

Pinned:

* the statement trace of an unconstrained run equals the unscoped run's, on
  the FTS-degraded lane and on the ANN ∪ FTS lane; a narrowed run, another
  member's Memory in the library, and a Deep Report phase all bind the list;
* a report phase under the default ceiling still recalls a source the scale
  index has not folded yet (``report_delta_fallback``);
* a lexical failure on the pushed-down ANN lane is still the supplementary
  arm's failure -- no ``chunk_fts`` banner;
* per producer, a source that appears after the verdict was taken is caught
  on read: chunk recall and the keyword arm re-run bound, the contribution
  hydrate drops it, ``_bound_on_drift`` re-runs the KG producer, and
  ``filter_retrieval_items`` flips the verdict for elements, chunks and KG
  items (never keeping an outsider's evidence);
* the check is against the ceiling taken BEFORE the read (TOCTOU), the
  verdict never goes back from "binds", a failing verdict probe binds, and
  the Stop reaches the verdict probes.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    filter_retrieval_items,
    record_ceiling_drift,
    run_ceiling_binds,
    scoped_allowed_source_ids,
    unbound_ceiling,
    verify_unbound_read,
)
from tests.model_testkit import bind_all_embedding_clients

TEXT = "bandgap reference topic body detail "


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.services.sqlite_repository import (
        SQLiteRepository, reset_request_user, set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    bob = instance.create_user("b00654321", "password-12")
    alice = instance.create_user("a00123456", "password-12")
    token = set_request_user(bob)
    try:
        yield instance, bob.id, alice.id
    finally:
        reset_request_user(token)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_source(repo, nb, texts) -> str:
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb, "Doc", "document", "s.md", "/tmp/s.md", 0, "h", "", "",
             "extracted", now, now),
        )
        for index, text in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{index:04d}", sid, "paragraph", f"p{index}", text,
                 "{}", now),
            )
    repo._chunk_and_embed_source(sid)
    repo.backfill_chunk_fts(nb)
    return sid


def _memory_source(repo, nb, owner) -> None:
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES ('mem-1',?,?,NULL,NULL,'ask_answer','confirmed','m','m',?,?)",
            (nb, owner, now, now),
        )
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,memory_id,"
            "created_at,updated_at) VALUES ('src-mem',?,'memory','memory','ready',"
            "'mem-1',?,?)",
            (nb, now, now),
        )


def _ceiling(repo, nb, owner, **kwargs):
    """The production default ceiling (verdict probes included)."""
    return default_ceiling_context(nb, owner, repo._runtime.ceiling_readers(), **kwargs)


@pytest.fixture(params=["fts_degraded", "ann"])
def library(request, repo, monkeypatch):
    instance, bob, alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    sid = _add_source(instance, nb, [f"{TEXT}{i} " * 5 for i in range(4)])
    candidates = instance.retrieval.candidates
    if request.param == "ann":
        instance.rebuild_unified_kg(nb)
        instance.build_scale_index(nb)
    else:
        monkeypatch.setattr(candidates, "notebook_copy_stats",
                            lambda _nb: {"copyable": False, "size": {}})
    statements: list = []
    real_connect = candidates._connect

    @contextlib.contextmanager
    def traced():
        with real_connect() as db:
            db.set_trace_callback(statements.append)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(candidates, "_connect", traced)
    return instance, nb, sid, bob, alice, statements


def _run_legs(repo, nb):
    candidates = repo.retrieval.candidates
    scored, _ids, _mat = candidates._retrieve_chunks(nb, "bandgap reference")
    keyword = candidates._keyword_chunk_candidates(nb, "bandgap")
    return ([(c.chunk_id, round(c.score, 9)) for c in scored],
            [(c.chunk_id, round(c.score, 9)) for c in keyword])


def _trace(statements, fn):
    statements.clear()
    result = fn()
    return result, list(statements)


def _binds_a_list(trace) -> bool:
    """A statement filtering chunks by a bound SOURCE list.  (Candidate
    hydration binds its own bounded chunk-id window on every run.)"""
    return any(
        "source_id IN (SELECT value FROM json_each" in statement
        for statement in trace
    )


def test_an_unconstrained_run_issues_the_unscoped_statements(library):
    repo, nb, _sid, bob, _alice, statements = library
    _run_legs(repo, nb)                   # warm language/probe caches

    unscoped, unscoped_trace = _trace(statements, lambda: _run_legs(repo, nb))
    with _ceiling(repo, nb, bob):
        _run_legs(repo, nb)               # the verdict's own reads, once
        scoped, scoped_trace = _trace(statements, lambda: _run_legs(repo, nb))
        assert scoped_allowed_source_ids(nb) is None

    assert unscoped_trace and not _binds_a_list(unscoped_trace)
    assert scoped_trace == unscoped_trace
    assert scoped == unscoped and scoped[0]


def test_a_narrowed_run_still_binds_the_list(library):
    repo, nb, sid, bob, _alice, statements = library
    _run_legs(repo, nb)
    narrowed = {"mode": "include", "source_ids": [sid], "narrowed": True}

    with _ceiling(repo, nb, bob, local_scope=narrowed):
        _result, trace = _trace(statements, lambda: _run_legs(repo, nb))

    assert _binds_a_list(trace)


def test_another_members_memory_makes_the_verdict_bind_the_list(library):
    repo, nb, _sid, bob, alice, statements = library
    _memory_source(repo, nb, bob)
    _run_legs(repo, nb)

    with _ceiling(repo, nb, alice):
        _result, trace = _trace(statements, lambda: _run_legs(repo, nb))
        assert run_ceiling_binds(current_source_scope(), nb) is True

    assert _binds_a_list(trace)


def test_a_report_run_keeps_binding_the_list(library):
    """The frozen list is also the ANN sidecar's coverage question in a
    report (sources a stale index does not hold are recalled by FTS), so a
    report phase binds it even when the verdict would push it down."""
    from app.services.retrieval_run import retrieval_run

    repo, nb, _sid, bob, _alice, statements = library
    _run_legs(repo, nb)

    with retrieval_run(run_kind="report_generation", actor_id=bob):
        with _ceiling(repo, nb, bob):
            _result, trace = _trace(statements, lambda: _run_legs(repo, nb))
            assert run_ceiling_binds(current_source_scope(), nb) is True

    assert _binds_a_list(trace)


def test_a_report_under_the_default_ceiling_recalls_an_unfolded_source(repo):
    """Quality review P1-A: a source uploaded after the last scale-index fold
    is not in the ANN; a report recalls it through the bounded
    ``report_delta_fallback`` FTS -- under the production default ceiling
    too, not only without a scope."""
    from app.services.retrieval_run import retrieval_run

    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    _add_source(instance, nb, ["indexed baseline text " * 20])
    instance.rebuild_unified_kg(nb)
    instance.build_scale_index(nb)
    delta = _add_source(instance, nb, ["DELTA9000 fresh unfolded evidence " * 20])
    idx = instance._scale_index(nb, allow_stale=True)
    query = "DELTA9000 unfolded evidence"
    candidates = instance.retrieval.candidates

    with retrieval_run(run_kind="report_generation", actor_id=bob):
        with _ceiling(instance, nb, bob):
            out = candidates._retrieve_chunks_ann(
                nb, query, candidates._embed_query(query), idx, recall=10,
            )

    assert out is not None
    assert delta in {chunk.source_id for chunk in out[0]}


def test_a_lexical_failure_on_the_pushed_down_ann_lane_raises_no_banner(repo, monkeypatch):
    """E2-2 follow-up 12: with the list pushed down, the ANN lane still knows
    its lexical half is the SUPPLEMENTARY arm (the frozen ceiling plans it in
    Python), so a failing FTS is no ``chunk_fts`` banner."""
    from app.models.ask import AskRequest
    from tests.model_testkit import bind_chat_client

    instance, _bob, _alice = repo
    query = "engram memory architecture"
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    _add_source(instance, nb, [f"{query} " * 20])
    instance.rebuild_unified_kg(nb)
    instance.build_scale_index(nb)
    instance.settings.query_rewrite_enabled = False
    instance.settings.chunk_kg_overlay_enabled = False

    class _Answer:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            return json.dumps({"answer": "Engram stores memory [k1].", "grounded": True})

        def chat(self, *_args, **_kwargs):
            return "Engram stores memory [k1]."

    bind_chat_client(instance, "ask_answer", _Answer())

    def _broken(*_args, **_kwargs):
        raise RuntimeError("secret database diagnostic")

    monkeypatch.setattr(instance._runtime.knowledge, "chunk_fts_search", _broken)
    events = []
    monkeypatch.setattr(instance.event_log, "emit", events.append)
    pushed_down = []
    real_unbound = unbound_ceiling

    def spy_unbound(notebook_id):
        ceiling = real_unbound(notebook_id)
        pushed_down.append(ceiling is not None)
        return ceiling

    monkeypatch.setattr("app.services.source_scope.unbound_ceiling", spy_unbound)

    # ``ask_chunk`` installs the default ceiling itself (``_retrieval_ceiling``).
    resp = instance.ask_chunk(nb, AskRequest(question=query, mode="chunk"))

    assert any(pushed_down), "the run really pushed the list down"
    roles = {e["recall_role"] for e in events if e.get("site") == "chunk_ann_union"}
    assert roles == {"supplement"}
    assert "chunk_fts" not in [e.stage for e in resp.model_errors]
    assert resp.answer


@pytest.mark.parametrize("leg", ["chunks", "keyword"])
def test_a_source_outside_the_freeze_is_caught_on_read_and_the_leg_reruns_bound(
    library, leg,
):
    repo, nb, sid, bob, _alice, statements = library
    candidates = repo.retrieval.candidates
    with _ceiling(repo, nb, bob):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False     # verdict taken first
        late = _add_source(repo, nb, [f"{TEXT} bandgap bandgap " * 6])
        if leg == "chunks":
            hits, trace = _trace(statements, lambda: candidates._retrieve_chunks(
                nb, "bandgap reference")[0])
        else:
            hits, trace = _trace(statements, lambda: candidates
                                 ._keyword_chunk_candidates(nb, "bandgap"))
        flipped = nb in scope._ceiling_bound_libraries

    assert hits and {hit.source_id for hit in hits} == {sid}
    assert late not in {hit.source_id for hit in hits}
    assert flipped
    assert _binds_a_list(trace), "the re-run binds the frozen list"


def test_the_contribution_hydrate_drops_a_late_source(repo):
    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    sid = _add_source(instance, nb, [f"{TEXT} one " * 5])
    candidates = instance.retrieval.candidates
    with _ceiling(instance, nb, bob):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False
        late = _add_source(instance, nb, [f"{TEXT} two " * 5])
        with candidates._connect() as db:
            chunk_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM chunks WHERE notebook_id=?", (nb,),
                ).fetchall()
            ]
        hydrated = candidates.hydrate_retrieval_contribution_chunks(nb, bob, chunk_ids)
        flipped = nb in scope._ceiling_bound_libraries

    assert hydrated and {chunk.source_id for chunk in hydrated} == {sid}
    assert late not in {chunk.source_id for chunk in hydrated}
    assert flipped


def test_bound_on_drift_reruns_the_kg_producer_after_a_flip(repo, monkeypatch):
    """``federated_retrieve`` reads the scope's own notebook without the list;
    an object whose evidence names a source outside the freeze flips the
    verdict in the boundary filter, and the producer is re-run bound."""
    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    sid = _add_source(instance, nb, [f"{TEXT} one " * 5])
    candidates = instance.retrieval.candidates
    calls = []

    def produce(*_args, **_kwargs):
        calls.append(scoped_allowed_source_ids(nb))
        return [
            {"object_id": "ko-in", "notebook_id": nb,
             "evidence": [{"source_id": sid, "quoted_span": "in"}]},
            {"object_id": "ko-late", "notebook_id": nb,
             "evidence": [{"source_id": "src-late", "quoted_span": "late"}]},
        ]

    monkeypatch.setattr(candidates, "_federated_retrieve_impl", produce)
    with _ceiling(instance, nb, bob):
        hits = candidates.federated_retrieve(nb, "bandgap")

    assert len(calls) == 2, "the producer re-runs once the verdict flipped"
    assert calls[0] is None and calls[1] is not None, "unbound first, bound after"
    assert [hit["object_id"] for hit in hits] == ["ko-in"]


def test_the_boundary_filter_flips_the_verdict_and_never_keeps_an_outsider(repo):
    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    sid = _add_source(instance, nb, [f"{TEXT} one " * 5])

    class _Item:
        def __init__(self, source_id):
            self.source_id = source_id
            self.notebook_id = ""

    for kind in ("element", "chunk"):
        with _ceiling(instance, nb, bob):
            scope = current_source_scope()
            assert run_ceiling_binds(scope, nb) is False
            kept = filter_retrieval_items(nb, kind, [_Item(sid), _Item("src-late")])
            assert [item.source_id for item in kept] == [sid]
            assert nb in scope._ceiling_bound_libraries, kind

    with _ceiling(instance, nb, bob):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False
        kept = filter_retrieval_items(nb, "knowledge", [
            {"object_id": "ko-mixed", "notebook_id": nb, "evidence": [
                {"source_id": sid, "quoted_span": "in"},
                {"source_id": "src-late", "quoted_span": "LATESECRET"},
            ]},
            {"object_id": "ko-late", "notebook_id": nb, "evidence": [
                {"source_id": "src-late", "quoted_span": "LATESECRET"},
            ]},
            {"object_id": "ko-bare", "notebook_id": nb, "evidence": []},
        ])
        assert nb in scope._ceiling_bound_libraries
    assert [item["object_id"] for item in kept] == ["ko-mixed"]
    assert "LATESECRET" not in json.dumps(kept)


def test_an_unbound_read_is_judged_against_the_ceiling_taken_before_it(repo):
    """TOCTOU: another thread flips the verdict between this producer's
    unbound read and its check -- the check still judges by the ceiling it
    captured, so the outsider is caught."""
    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    sid = _add_source(instance, nb, [f"{TEXT} one " * 5])
    with _ceiling(instance, nb, bob):
        scope = current_source_scope()
        captured = unbound_ceiling(nb)
        assert captured is not None and sid in captured
        record_ceiling_drift(scope, nb)                  # the racing flip
        assert verify_unbound_read(nb, captured, [sid, "src-late"]) is False
        assert verify_unbound_read(nb, captured, [sid]) is True
        assert verify_unbound_read(nb, None, ["src-late"]) is True


def test_the_verdict_never_goes_back_from_binds(repo):
    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    _add_source(instance, nb, [f"{TEXT} one " * 5])
    with _ceiling(instance, nb, bob):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False
        record_ceiling_drift(scope, nb)
        scope._ceiling_binds_memo[nb] = False            # a stale racing write
        assert run_ceiling_binds(scope, nb) is True
        assert scoped_allowed_source_ids(nb) is not None


def test_a_failing_verdict_probe_binds_and_a_stop_propagates(repo):
    from dataclasses import replace

    from app.services.cancellation import AskCancelled
    from app.services.source_scope import CeilingVerdictProbes

    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    _add_source(instance, nb, [f"{TEXT} one " * 5])
    readers = instance._runtime.ceiling_readers()

    def pool_timeout(*_args):
        raise RuntimeError("pool timeout")

    failing = replace(readers, verdict_probes=CeilingVerdictProbes(
        universe_digests=pool_timeout, foreign_hidden=lambda *_a: False,
    ))
    with default_ceiling_context(nb, bob, failing):
        assert run_ceiling_binds(current_source_scope(), nb) is True
        assert scoped_allowed_source_ids(nb) is not None

    def stopped(*_args):
        raise AskCancelled()

    stopping = replace(readers, verdict_probes=CeilingVerdictProbes(
        universe_digests=stopped, foreign_hidden=lambda *_a: False,
    ))
    with default_ceiling_context(nb, bob, stopping):
        with pytest.raises(AskCancelled):
            run_ceiling_binds(current_source_scope(), nb)


def test_the_stop_reaches_the_verdict_probes():
    import threading

    from app.repositories.read_budget import current_read_budget
    from app.services.source_scope import (
        CeilingReaders, CeilingVerdictProbes, cancellable_ceiling_readers,
    )

    seen = []

    def probe(*_args):
        budget = current_read_budget()
        seen.append(None if budget is None else budget.cancel_event)
        return ["", ""]

    cancel = threading.Event()
    readers = CeilingReaders(
        participants=lambda nb: (nb,), visible=lambda nb: (),
        hidden=lambda nb, owner: (), memory_sources=lambda nb: (),
        verdict_probes=CeilingVerdictProbes(
            universe_digests=probe, foreign_hidden=lambda *_a: probe() and False,
        ),
    )
    wrapped = cancellable_ceiling_readers(readers, cancel)
    wrapped.verdict_probes.universe_digests("nb", "u")
    wrapped.verdict_probes.foreign_hidden("nb", "u")
    assert seen == [cancel, cancel]


def test_a_probe_less_scope_answers_source_ceiling_binds_without_the_memo():
    """E2-2's contract (follow-up 14): a scope with no verdict probes (every
    installer but the store-wired default ceiling) answers exactly
    ``source_ceiling_binds`` and never reads the run memo -- a stale "False"
    left there cannot unbind it."""
    from app.services.source_scope import source_scope_context

    frozen = {"mode": "include", "source_ids": ["s1"], "narrowed": False}
    with source_scope_context("nb", frozen):
        scope = current_source_scope()
        scope._ceiling_binds_memo["nb"] = False
        assert scope._verdict_probes is None
        assert run_ceiling_binds(scope, "nb") is scope.source_ceiling_binds("nb") is True
        assert unbound_ceiling("nb") is None
        assert scoped_allowed_source_ids("nb") == ("s1",)
        assert run_ceiling_binds(scope, "other-library") is scope.source_ceiling_binds(
            "other-library")


def test_the_node_context_verdict_reads_the_one_row_fingerprint(repo, monkeypatch):
    """The node-context re-read's verdict (``NodeContextCeilingVerdict``)
    judges drift by the store's one-row fingerprint -- one visible read with
    ``digest_for_owner`` -- not by the two full reads it used before."""
    from app.services.kg_viewer_scope import NodeContextCeilingVerdict

    instance, bob, _alice = repo
    nb = instance.create_notebook(NotebookCreate(name="kb")).id
    _add_source(instance, nb, [f"{TEXT} one " * 5])
    store = instance._runtime.source_store
    reads = []
    real_visible, real_hidden = store.all_visible_source_ids, store.hidden_source_ids

    def visible(notebook_id, *args, **kwargs):
        reads.append(("visible", "digest" if kwargs.get("digest_for_owner") else "full"))
        return real_visible(notebook_id, *args, **kwargs)

    def hidden(*args, **kwargs):
        reads.append(("hidden", "full"))
        return real_hidden(*args, **kwargs)

    verdict = NodeContextCeilingVerdict(database=instance._runtime.database, sources=store)
    with _ceiling(instance, nb, bob):
        monkeypatch.setattr(store, "all_visible_source_ids", visible)
        monkeypatch.setattr(store, "hidden_source_ids", hidden)
        assert verdict(nb) is False
    assert ("visible", "digest") in reads
    assert ("visible", "full") not in reads
