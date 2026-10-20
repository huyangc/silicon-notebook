"""The rebuild's paged cluster-map write on PostgreSQL: the twin of
``tests/test_cluster_map_write_paging.py``. Same world, same checks, same
expectations (``tests/cluster_map_write_cases.py``). The plan pins live in
``tests/postgres/test_cluster_map_write_plan_pins.py``."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.postgres.repository import PostgresRepository
from app.services.embedding import FakeEmbedder
from tests import cluster_map_write_cases as cases
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_cluster_map_write"),
]


@pytest.fixture
def repo(postgres_settings: Settings):
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def test_paged_write_matches_one_shot_row_for_row_pg(repo):
    cases.check_paged_write_matches_one_shot(repo, postgres=True)


def test_pass_b_reads_vectors_in_scratch_insertion_order_pg(repo):
    """Same order on PostgreSQL, and row for row the output of the statement
    as it was before it ordered by ``s.ordinal`` (it recovered the order
    through ``knowledge_objects.ordinal``): switching the sort key changes no
    row and no position."""
    nb, rows = cases.check_pass_b_reads_vectors_in_scratch_insertion_order(
        repo, postgres=True)
    with repo._connect() as db:
        before = [(r["seed"], bytes(r["vector"])) for r in db.execute(
            "SELECT s.seed AS seed, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
            "  AND s.notebook_id=e.notebook_id AND s.run_id=%s "
            "JOIN knowledge_objects k ON k.id=s.object_id "
            "  AND k.notebook_id=s.notebook_id "
            "WHERE e.notebook_id=%s ORDER BY k.ordinal", (cases.RUN, nb)).fetchall()]
    assert rows == before


def test_rebuild_pages_and_a_lost_claim_stops_between_pages_pg(repo, monkeypatch):
    cases.check_rebuild_pages_and_a_lost_claim_stops_between_pages(
        repo, postgres=True, monkeypatch=monkeypatch)


def test_each_page_runs_under_the_maintenance_statement_timeout_floor(repo):
    """A page's statements run with statement_timeout raised to the 600 s
    maintenance floor (transaction-local, #930's helper): a page is
    ``page_rows`` rows of concept_clusters index maintenance, and a slow disk
    must not turn that into a failed rebuild. The pool's own timeout comes
    back after the transaction. This only sees the setting once the call
    returns; that the floor is raised BEFORE the bound read and the INSERT (on
    a non-empty, multi-page run) is pinned per statement in
    ``test_cluster_map_write_plan_pins._captured``."""
    store = repo._runtime.unified_kg
    nb = repo.create_notebook(cases.NotebookCreate(name="floor")).id
    with repo._write() as db:
        db.execute("SET LOCAL statement_timeout = '1s'")
        store.write_cluster_map_generation(
            db, nb, "concept", "run-floor", cases.CREATED, 3,
            start=None, page_rows=10)
        inside = db.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
    with repo._connect() as db:
        after = db.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
    assert inside == "10min"
    assert after != "10min"
