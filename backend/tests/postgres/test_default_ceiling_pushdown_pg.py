"""PostgreSQL twin of ``tests/test_default_ceiling_pushdown.py``.

The plan-cache cliff this closes: a statement that binds a ~49k-id source list
is prepared by psycopg from its 5th execution and PostgreSQL may switch it to a
generic plan from about the 11th, where the array is an opaque parameter
(measured 1.1 s -> 3.1 s for chunk FTS at 49k sources).  A run whose default
ceiling cannot exclude anything binds NO list on its chunk legs.

All scopes are the PRODUCTION default ceiling (``default_ceiling_context`` over
``RepositoryRuntime.ceiling_readers()``, verdict probes included):

* 15 executions of the keyword arm and the chunk lane under an unconstrained
  run issue, every time, exactly the unscoped run's statements -- no
  ``source_id=ANY(...)`` predicate at all -- and no list-bound statement is in
  the plan cache;
* a narrowed run (the control) binds the list, never through the plan cache;
* another member's Memory in the library binds the list;
* a report phase binds the list;
* a source outside the freeze that appears after the verdict is caught on read
  and the leg re-runs bound.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import (
    current_source_scope,
    default_ceiling_context,
    run_ceiling_binds,
    scoped_allowed_source_ids,
)
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_default_ceiling_pushdown"),
]

T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)
EXECUTIONS = 15


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    settings = postgres_settings.model_copy(update={
        "postgres_pool_min_size": 1, "postgres_pool_max_size": 1,
    })
    repository = PostgresRepository(settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


class _Recorder:
    """Delegating connection proxy that records every statement text."""

    def __init__(self, connection, sink):
        self._connection = connection
        self._sink = sink

    def execute(self, statement, params=None, **kwargs):
        text = statement if isinstance(statement, str) else statement.as_string(
            self._connection)
        self._sink.append(text)
        return self._connection.execute(statement, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _insert_source(repo, nb, sid, text, *, memory_id=None) -> None:
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,memory_id,created_at,updated_at) "
            "VALUES (%s,%s,'paper',%s,'extracted','parsed',%s,%s,%s)",
            (sid, nb, "memory" if memory_id else "markdown", memory_id, T0, T0),
        )
        for index in range(4):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (%s,%s,%s,%s,'',"
                "'[]'::jsonb,%s)",
                (f"c-{sid}-{index}", nb, sid, f"{text} {index} " * 5, T0),
            )


@pytest.fixture
def library(repo, monkeypatch):
    bob = repo.create_user("b00654321", "pw123456")
    alice = repo.create_user("a00123456", "pw123456")
    token = set_request_user(bob)
    nb = repo.create_notebook(NotebookCreate(name="kb")).id
    _insert_source(repo, nb, "src-doc", "bandgap reference detail")
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "notebook_copy_stats",
                        lambda _nb: {"copyable": False, "size": {}})
    statements: list = []
    real_connect = candidates._connect

    @contextlib.contextmanager
    def recorded():
        with real_connect() as db:
            yield _Recorder(db, statements)

    monkeypatch.setattr(candidates, "_connect", recorded)
    try:
        yield repo, nb, bob.id, alice.id, statements
    finally:
        reset_request_user(token)


def _ceiling(repo, nb, owner, **kwargs):
    return default_ceiling_context(nb, owner, repo._runtime.ceiling_readers(), **kwargs)


def _legs(repo, nb):
    candidates = repo.retrieval.candidates
    scored = candidates._retrieve_chunks(nb, "bandgap reference")[0]
    keyword = candidates._keyword_chunk_candidates(nb, "bandgap")
    return [c.chunk_id for c in scored], [c.chunk_id for c in keyword]


def _source_list_bound(trace) -> bool:
    return any("source_id=ANY(" in statement for statement in trace)


def _prepared(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        return [row["statement"] for row in db.execute(
            "SELECT statement FROM pg_prepared_statements").fetchall()]


def test_pg_fifteen_unconstrained_executions_issue_only_unscoped_statements(library):
    repo, nb, bob, _alice, statements = library
    _legs(repo, nb)
    statements.clear()
    unscoped = _legs(repo, nb)
    unscoped_trace = list(statements)

    traces = []
    with _ceiling(repo, nb, bob):
        _legs(repo, nb)                   # the verdict's own reads, once
        assert scoped_allowed_source_ids(nb) is None
        for _ in range(EXECUTIONS):
            statements.clear()
            assert _legs(repo, nb) == unscoped
            traces.append(list(statements))

    assert unscoped_trace and unscoped[0] and unscoped[1]
    assert not _source_list_bound(unscoped_trace)
    assert all(trace == unscoped_trace for trace in traces)
    assert not any("source_id=ANY(" in text for text in _prepared(repo))


def test_pg_a_narrowed_run_binds_the_list_without_caching_its_plan(library):
    repo, nb, bob, _alice, statements = library
    _legs(repo, nb)
    narrowed = {"mode": "include", "source_ids": ["src-doc"], "narrowed": True}

    with _ceiling(repo, nb, bob, local_scope=narrowed):
        for _ in range(EXECUTIONS):
            statements.clear()
            _legs(repo, nb)
            assert _source_list_bound(statements)
    assert not any("source_id=ANY(" in text for text in _prepared(repo))


def test_pg_another_members_memory_binds_the_list(library):
    repo, nb, bob, alice, statements = library
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES ('mem-1',%s,%s,NULL,NULL,'ask_answer','confirmed','m','m',%s,%s)",
            (nb, bob, T0, T0),
        )
    _insert_source(repo, nb, "src-mem", "bandgap memory note", memory_id="mem-1")
    _legs(repo, nb)

    with _ceiling(repo, nb, alice):
        statements.clear()
        scored, keyword = _legs(repo, nb)
        assert run_ceiling_binds(current_source_scope(), nb) is True

    assert _source_list_bound(statements)
    assert not any(cid.startswith("c-src-mem") for cid in scored + keyword)


def test_pg_a_report_run_keeps_binding_the_list(library):
    from app.services.retrieval_run import retrieval_run

    repo, nb, bob, _alice, statements = library
    _legs(repo, nb)

    with retrieval_run(run_kind="report_generation", actor_id=bob):
        with _ceiling(repo, nb, bob):
            statements.clear()
            _legs(repo, nb)
            assert run_ceiling_binds(current_source_scope(), nb) is True

    assert _source_list_bound(statements)


@pytest.mark.parametrize("leg", ["chunks", "keyword"])
def test_pg_a_source_outside_the_freeze_is_caught_on_read(library, leg):
    repo, nb, bob, _alice, statements = library
    candidates = repo.retrieval.candidates
    with _ceiling(repo, nb, bob):
        scope = current_source_scope()
        assert run_ceiling_binds(scope, nb) is False     # verdict taken first
        _insert_source(repo, nb, "src-late", "bandgap bandgap bandgap reference")
        statements.clear()
        if leg == "chunks":
            hits = candidates._retrieve_chunks(nb, "bandgap reference")[0]
        else:
            hits = candidates._keyword_chunk_candidates(nb, "bandgap")
        flipped = nb in scope._ceiling_bound_libraries

    assert hits and {hit.source_id for hit in hits} == {"src-doc"}
    assert flipped
    assert _source_list_bound(statements), "the re-run binds the frozen list"
