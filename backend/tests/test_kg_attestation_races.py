"""PR-D D5 / D6 / D4 minting on SQLite: the graph-side citation producers
register what they cite, and the real terminal read judges it.

* KG objects (``knowledge_context`` anchors, ``citations_from`` cards) and
  derived chains (``ReasoningRetriever.follow_chain``) pointer-register the
  element they cite -- ONE batched read per call, over what was admitted;
* ``collection_item_citations`` hashes the full text its own hydration read
  returned and asks only about an element row whose hydration missed.

Every scenario lives in ``tests/kg_attestation_testkit.py`` and is run
verbatim by the PostgreSQL twin ``tests/postgres/test_kg_attestation_races_pg.py``.
Outside a global run nothing is registered and nothing is read (J8), pinned at
the end of this file.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from tests import kg_attestation_testkit as kit


@pytest.fixture
def store(tmp_path):
    from app.models.notebooks import NotebookCreate
    from app.repositories.sqlite.source_store import SourceStore
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'kg-attest.db'}",
        storage_dir=str(tmp_path / "storage"),
    ))
    notebook = repo.create_notebook(NotebookCreate(name="kg-attest"))
    database = repo._runtime.source_store.database
    sources = object.__new__(SourceStore)
    sources.database = database
    try:
        yield sources, database, notebook.id
    finally:
        repo.close()


MUTATIONS = sorted(kit.RACE_EXPECTATIONS)


# --- KG objects --------------------------------------------------------------

@pytest.mark.parametrize("mutation", MUTATIONS)
def test_kg_object_card_and_anchor_race_the_terminal_read(store, mutation):
    sources, database, notebook_id = store
    kit.kg_object_race(sources, database, "?", notebook_id, mutation)


def test_kg_object_anchor_is_registered_by_knowledge_context_itself(store):
    kit.kg_anchor_registered_on_its_own(*store[:2], "?", store[2])


def test_kg_object_dangling_pointer_is_not_minted(store):
    kit.kg_dangling_pointer_is_not_minted(*store[:2], "?", store[2])


@pytest.mark.parametrize("mutation", sorted(kit.MULTI_OCCURRENCE_EXPECTATIONS))
def test_kg_object_registers_exactly_the_occurrence_it_writes(store, mutation):
    sources, database, notebook_id = store
    kit.kg_registers_the_occurrence_it_writes(
        sources, database, "?", notebook_id, mutation,
    )


def test_knowledge_context_issues_one_read_over_admitted_objects(store):
    """Also the rebase guard for ``_attest_kg_anchor_evidence(evidence_by_id)``
    staying AFTER ``_admit``: moved before it, nothing admitted is read."""
    kit.kg_one_read_per_call(*store[:2], "?", store[2])


def test_collection_registers_only_the_citations_it_minted(store):
    kit.collection_registers_only_what_it_minted(*store[:2], "?", store[2])


def test_knowledge_context_read_is_one_statement_on_the_real_store(store):
    """The one batched read is one SQL statement at this size (the store
    batches by ``IN_CHUNK``)."""
    sources, database, notebook_id = store
    kit.seed(database, "?", notebook_id)
    statements: list = []
    real_connect = database.connect

    from contextlib import contextmanager

    @contextmanager
    def counting_connect(*args, **kwargs):
        with real_connect(*args, **kwargs) as db:
            db.set_trace_callback(
                lambda sql: statements.append(sql)
                if "FROM source_elements" in sql and "text" in sql else None
            )
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    pool = [kit.kg_hit(f"ko-{i}", kit.CITED, notebook_id) for i in range(12)]
    service = kit.evidence_service(sources, notebook_id, kit.Knowledge({
        hit.object_id: [kit.occurrence(kit.CITED)] for hit in pool
    }))
    database.connect = counting_connect
    try:
        with kit.global_run(sources, notebook_id) as run:
            service.knowledge_context(notebook_id, pool)
    finally:
        database.connect = real_connect
    assert len(run.reader.reads) == 1
    assert len(statements) == 1


# --- follow_chain ------------------------------------------------------------

@pytest.mark.parametrize("mutation", MUTATIONS)
def test_follow_chain_hop_anchors_race_the_terminal_read(store, mutation):
    sources, database, notebook_id = store
    kit.chain_race(sources, database, "?", notebook_id, mutation)


def test_follow_chain_dangling_primary_is_not_minted(store):
    kit.chain_dangling_pointer_is_not_minted(*store[:2], "?", store[2])


def test_follow_chain_registration_never_mutates_its_input(store):
    kit.chain_registration_never_mutates_its_input(*store[:2], "?", store[2])


# --- collection_item_citations -----------------------------------------------

@pytest.mark.parametrize("mutation", MUTATIONS)
def test_enumerated_row_citations_race_the_terminal_read(store, mutation):
    sources, database, notebook_id = store
    kit.collection_race(sources, database, "?", notebook_id, mutation)


def test_enumerated_dangling_element_row_is_not_minted(store):
    kit.collection_dangling_element_row(*store[:2], "?", store[2])


def test_enumerated_row_registered_then_deleted_is_minted_and_source_gone(store):
    kit.collection_registered_row_deleted_before_minting(*store[:2], "?", store[2])


def test_kg_element_registered_then_deleted_keeps_card_and_anchor(store):
    kit.kg_registered_element_deleted_before_the_pointer_read(*store[:2], "?", store[2])


def test_follow_chain_element_registered_then_deleted_keeps_its_locator(store):
    kit.chain_registered_element_deleted_before_the_pointer_read(*store[:2], "?", store[2])


# --- outside a global run: byte-identical, zero reads (J8) --------------------

def test_single_notebook_producers_register_nothing_and_read_nothing(store):
    """No plan installed: every producer returns what it returned before PR-D,
    including a dangling pointer, and the attestation seam is never asked."""
    sources, database, notebook_id = store
    kit.seed(database, "?", notebook_id)
    citations, anchors, id_map = kit.kg_object_references(
        sources, notebook_id, kit.DANGLING,
    )
    assert [c.element_id for c in citations] == [kit.DANGLING]
    assert [a.element_id for a in anchors] == [kit.DANGLING]
    assert id_map["k1"]["element_id"] == kit.DANGLING
    result = kit.chain_result(notebook_id, (kit.DANGLING, kit.OTHER))
    inferences = result.inferences
    assert kit.follow_chain_via_reasoning(result).inferences is inferences
