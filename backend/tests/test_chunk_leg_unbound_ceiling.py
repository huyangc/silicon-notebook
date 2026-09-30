"""Correction 4b (P2-C2): chunk legs of an unconstrained run bind no id list.

A frozen all-selected ceiling on a library with nothing drifted and no other
member's Memory cannot exclude anything (the run's ``ceiling_binds`` verdict is
False), so the chunk recall lane (ANN / FTS) and the keyword arm issue the
unscoped run's statements -- byte for byte, no ``json_each`` / 49k-id list --
and verify what comes back.  Pinned here on a real SQLite store:

* the statement trace of an unconstrained scoped run equals the unscoped
  run's, on the FTS-degraded lane and on the ANN ∪ FTS lane; a narrowed run
  (the control) does bind the list;
* another member's Memory in the library makes the verdict bind the list;
* a source outside the freeze that appears after the verdict was taken is
  caught on read: the drift is recorded and the leg re-runs bound, so the
  late source's passage is not returned.

The PostgreSQL twin (15 executions, the plan-cache cliff) is
``tests/postgres/test_chunk_leg_unbound_ceiling_pg.py``.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import current_source_scope, source_scope_context
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


def _scope(visible, owner, *, narrowed=False):
    return {"mode": "include", "source_ids": list(visible),
            "hidden_source_ids": [], "narrowed": narrowed, "owner_id": owner}


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
        # No index and a "large" library: the bounded FTS-degraded lane.
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
    """A statement that filters chunks by a bound SOURCE list (the ceiling).
    ``json_each`` alone is not the test: candidate hydration binds its own
    bounded chunk-id window that way on every run, scoped or not."""
    return any(
        "source_id IN (SELECT value FROM json_each" in statement
        for statement in trace
    )


def test_an_unconstrained_run_issues_the_unscoped_statements(library):
    repo, nb, sid, bob, _alice, statements = library
    _run_legs(repo, nb)                   # warm language/probe caches

    unscoped, unscoped_trace = _trace(statements, lambda: _run_legs(repo, nb))
    with source_scope_context(nb, _scope([sid], bob)):
        scoped, scoped_trace = _trace(statements, lambda: _run_legs(repo, nb))

    assert unscoped_trace and not _binds_a_list(unscoped_trace)
    assert scoped_trace == unscoped_trace
    assert scoped == unscoped and scoped[0]


def test_a_narrowed_run_still_binds_the_list(library):
    repo, nb, sid, bob, _alice, statements = library
    _run_legs(repo, nb)

    with source_scope_context(nb, _scope([sid], bob, narrowed=True)):
        _result, trace = _trace(statements, lambda: _run_legs(repo, nb))

    assert _binds_a_list(trace)


def test_another_members_memory_makes_the_verdict_bind_the_list(library):
    """Alice asks; Bob's confirmed Memory is in the library.  The all-selected
    freeze excludes it, an unscoped statement would not -- so the verdict's
    foreign-hidden arm binds and the list is pushed."""
    repo, nb, sid, bob, alice, statements = library
    _memory_source(repo, nb, bob)
    _run_legs(repo, nb)

    with source_scope_context(nb, _scope([sid], alice)):
        _result, trace = _trace(statements, lambda: _run_legs(repo, nb))

    assert _binds_a_list(trace)


def test_a_report_run_keeps_binding_the_list(library):
    """Reports keep the frozen list: there it also answers which sources a
    stale ANN generation does not hold (they are recalled by FTS), so an
    unbound report would silently lose them."""
    from app.services.retrieval_run import retrieval_run

    repo, nb, sid, bob, _alice, statements = library
    _run_legs(repo, nb)

    with retrieval_run(run_kind="report_generation", actor_id=bob):
        with source_scope_context(nb, _scope([sid], bob)):
            _result, trace = _trace(statements, lambda: _run_legs(repo, nb))

    assert _binds_a_list(trace)


@pytest.mark.parametrize("leg", ["chunks", "keyword"])
def test_a_source_outside_the_freeze_is_caught_on_read_and_the_leg_reruns_bound(
    library, leg,
):
    """The verdict was taken (and said "does not bind") before a new source
    finished extracting; the unbound statement returns its passage.  The
    check on read records the drift, the leg re-runs with the frozen list,
    and the late passage is not returned."""
    repo, nb, sid, bob, _alice, statements = library
    candidates = repo.retrieval.candidates
    with source_scope_context(nb, _scope([sid], bob)):
        scope = current_source_scope()
        scope._ceiling_binds_memo[nb] = False       # verdict taken earlier
        late = _add_source(repo, nb, [f"{TEXT} bandgap bandgap " * 6])
        if leg == "chunks":
            hits, trace = _trace(statements, lambda: candidates._retrieve_chunks(
                nb, "bandgap reference")[0])
        else:
            hits, trace = _trace(statements, lambda: candidates
                                 ._keyword_chunk_candidates(nb, "bandgap"))
        drift_recorded = scope._ceiling_binds_memo[nb]

    assert hits and {hit.source_id for hit in hits} == {sid}
    assert late not in {hit.source_id for hit in hits}
    assert drift_recorded is True
    assert _binds_a_list(trace), "the re-run binds the frozen list"
