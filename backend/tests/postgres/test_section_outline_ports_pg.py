"""PostgreSQL twin of ``tests/test_section_outline_ports.py``: the section
outline / similarity / read ports over a real PostgreSQL store (``ordinal`` as
document order, ``vector_rows_for_ids`` over ``bytea`` vectors, the
``jsonb`` element ids normalised by the adapter), plus the outline scan's plan
on ``idx_chunks_source_ordinal``."""
from __future__ import annotations

import pytest

from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients
from tests.test_section_outline_ports import CHECKS, T0, SqliteSeed, _new_notebook

pytestmark = [pytest.mark.postgres_integration]


class PgSeed(SqliteSeed):
    ph = "%s"

    def _write(self):
        return self.repo._runtime.database.write()

    def _ts(self):
        return T0

    def _json(self):
        return "%s::jsonb"

    def source(self, nb, sid, title="Spec Report", kind="markdown"):
        with self._write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'extracted','parsed',%s,%s)",
                (sid, nb, title, "markdown" if kind == "md" else kind, T0, T0),
            )


@pytest.fixture
def backend(postgres_settings):
    from app.core.request_context import reset_request_user, set_request_user
    from app.services.postgres_repository import PostgresRepository

    repo = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    user = repo._runtime.identity.create_user("b00654321", "password-12")
    token = set_request_user(user)
    try:
        yield repo, PgSeed(repo)
    finally:
        reset_request_user(token)
        repo.close()


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
def test_pg(backend, monkeypatch, check):
    repo, seed = backend
    check(repo, seed, monkeypatch)


class _Recording:
    """The adapter's connection, recording every statement it sends."""

    def __init__(self, connection):
        self._connection = connection
        self.sent: list = []

    def execute(self, statement, params=None, **options):
        self.sent.append((statement, params))
        return self._connection.execute(statement, params, **options)


def test_pg_outline_scan_is_an_ordered_walk_of_the_source_ordinal_index(backend):
    """The plan of the statement the ADAPTER sends (recorded, then EXPLAINed
    with its own parameters), not of a copy of it: bounded and sort-free -- the
    ``LIMIT`` can stop an index walk in ``ordinal`` order instead of sorting
    the source's rows.  A near-empty table rightly prefers a bitmap scan plus
    a tiny sort, so the alternatives are penalised and the question is whether
    the ordered index path EXISTS (the ``test_core_store_conformance``
    technique); without it the plan would still sort."""
    repo, seed = backend
    nb = _new_notebook(repo, "spec")
    seed.report(nb, "src-r")
    with repo._runtime.database.connect() as db:
        recording = _Recording(db)
        rows, truncated = repo.retrieval.candidates.chunks.source_outline_rows(
            recording, nb, "src-r", 3)
        assert [row["id"] for row in rows] == ["c-toc", "c-571", "c-572"]
        assert truncated is True
        ((statement, params),) = recording.sent
        for knob in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            db.execute(f"SET LOCAL {knob}=off")
        plan = "\n".join(
            str(next(iter(dict(row).values())))
            for row in db.execute(f"EXPLAIN {statement}", params).fetchall()
        )
    assert "idx_chunks_source_ordinal" in plan, plan
    assert "Sort" not in plan, plan
