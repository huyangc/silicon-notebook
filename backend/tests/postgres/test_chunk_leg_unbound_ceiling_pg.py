"""PostgreSQL twin of tests/test_chunk_leg_unbound_ceiling.py (correction 4b).

The plan-cache cliff this closes: a statement that binds a ~49k-id source list
is prepared by psycopg from its 5th execution and PostgreSQL may switch it to a
generic plan from about the 11th, where the array is an opaque parameter
(measured 1.1 s -> 3.1 s for chunk FTS at 49k sources).  PR-F made every bound
list a custom-planned ``execute_ids`` statement; this goes one step further for
a run whose ceiling cannot exclude anything: its chunk legs bind NO list.

* 15 executions of the keyword arm and the chunk lane under an unconstrained
  all-selected freeze issue, every time, exactly the unscoped run's statements
  -- no ``source_id=ANY(...)`` predicate at all, so there is no list for a
  generic plan to mis-estimate -- and those statements do enter the plan cache
  like any unscoped statement;
* a narrowed run (the control) binds the list, never through the plan cache;
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
from app.services.source_scope import current_source_scope, source_scope_context
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_chunk_leg_unbound_ceiling"),
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


@pytest.fixture
def library(repo, monkeypatch):
    bob = repo.create_user("b00654321", "pw123456")
    alice = repo.create_user("a00123456", "pw123456")
    token = set_request_user(bob)
    nb = repo.create_notebook(NotebookCreate(name="kb")).id
    with repo._runtime.database.write() as db:
        for sid in ("src-doc",):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES (%s,%s,'paper','markdown','extracted','parsed',%s,%s)",
                (sid, nb, T0, T0),
            )
        for index in range(4):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (%s,%s,'src-doc',%s,'',"
                "'[]'::jsonb,%s)",
                (f"c-doc-{index}", nb, f"bandgap reference detail {index} " * 5, T0),
            )
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


def _scope(visible, owner, *, narrowed=False):
    return {"mode": "include", "source_ids": list(visible),
            "hidden_source_ids": [], "narrowed": narrowed, "owner_id": owner}


def _legs(repo, nb):
    candidates = repo.retrieval.candidates
    scored = candidates._retrieve_chunks(nb, "bandgap reference")[0]
    keyword = candidates._keyword_chunk_candidates(nb, "bandgap")
    return [c.chunk_id for c in scored], [c.chunk_id for c in keyword]


def _source_list_bound(trace) -> bool:
    return any("source_id=ANY(" in statement for statement in trace)


def test_pg_fifteen_unconstrained_executions_issue_only_unscoped_statements(library):
    repo, nb, bob, _alice, statements = library
    _legs(repo, nb)
    statements.clear()
    unscoped = _legs(repo, nb)
    unscoped_trace = list(statements)

    traces = []
    with source_scope_context(nb, _scope(["src-doc"], bob)):
        for _ in range(EXECUTIONS):
            statements.clear()
            assert _legs(repo, nb) == unscoped
            traces.append(list(statements))

    assert unscoped_trace and unscoped[0] and unscoped[1]
    assert not _source_list_bound(unscoped_trace)
    assert all(trace == unscoped_trace for trace in traces)
    with repo._runtime.database.connect() as db:
        prepared = [row["statement"] for row in db.execute(
            "SELECT statement FROM pg_prepared_statements").fetchall()]
    assert not any("source_id=ANY(" in text for text in prepared)


def test_pg_a_narrowed_run_binds_the_list_without_caching_its_plan(library):
    repo, nb, bob, _alice, statements = library
    _legs(repo, nb)

    with source_scope_context(nb, _scope(["src-doc"], bob, narrowed=True)):
        for _ in range(EXECUTIONS):
            statements.clear()
            _legs(repo, nb)
            assert _source_list_bound(statements)
    with repo._runtime.database.connect() as db:
        prepared = [row["statement"] for row in db.execute(
            "SELECT statement FROM pg_prepared_statements").fetchall()]
    assert not any("source_id=ANY(" in text for text in prepared)


def test_pg_a_source_outside_the_freeze_is_caught_on_read(library):
    repo, nb, bob, _alice, statements = library
    candidates = repo.retrieval.candidates
    with source_scope_context(nb, _scope(["src-doc"], bob)):
        scope = current_source_scope()
        scope._ceiling_binds_memo[nb] = False       # the verdict, taken earlier
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES ('src-late',%s,'late','markdown','extracted','parsed',%s,%s)",
                (nb, T0, T0),
            )
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES ('c-late',%s,'src-late',%s,'',"
                "'[]'::jsonb,%s)",
                (nb, "bandgap bandgap bandgap reference " * 6, T0),
            )
        statements.clear()
        hits = candidates._keyword_chunk_candidates(nb, "bandgap")
        drift_recorded = scope._ceiling_binds_memo[nb]

    assert hits and {hit.source_id for hit in hits} == {"src-doc"}
    assert drift_recorded is True
    assert _source_list_bound(statements)
