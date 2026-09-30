"""PR-D D5 / D6 / D4 minting -- PostgreSQL twin of ``tests/test_kg_attestation_races.py``.

The same scenarios (``tests/kg_attestation_testkit.py``) over the same rows,
against the PostgreSQL source store: the pointer reads and the terminal read
hash element bodies IN SQL here, while ``collection_item_citations`` hashes the
text its hydration read returned in Python -- so a twin is what pins that the
two digests of one element agree on the primary backend.
"""
from __future__ import annotations

import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from tests import kg_attestation_testkit as kit


pytestmark = pytest.mark.postgres_integration

_NOTEBOOK = "nb-kg-attest"
MUTATIONS = sorted(kit.RACE_EXPECTATIONS)


@pytest.fixture
def store(postgres_database):
    from app.repositories.postgres.source_store import SourceStore

    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("kg-attest-owner", "kg-attest@example.test", "KG", "user", "active",
             kit.NOW, kit.NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (_NOTEBOOK, "KG attest", "kg-attest-owner", kit.NOW, kit.NOW),
        )
    sources = object.__new__(SourceStore)
    sources.database = postgres_database
    return sources, postgres_database, _NOTEBOOK


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_kg_object_card_and_anchor_race_the_terminal_read_pg(store, mutation):
    sources, database, notebook_id = store
    kit.kg_object_race(sources, database, "%s", notebook_id, mutation)


def test_kg_object_anchor_is_registered_by_knowledge_context_itself_pg(store):
    kit.kg_anchor_registered_on_its_own(*store[:2], "%s", store[2])


def test_kg_object_dangling_pointer_is_not_minted_pg(store):
    kit.kg_dangling_pointer_is_not_minted(*store[:2], "%s", store[2])


@pytest.mark.parametrize("mutation", sorted(kit.MULTI_OCCURRENCE_EXPECTATIONS))
def test_kg_object_registers_exactly_the_occurrence_it_writes_pg(store, mutation):
    sources, database, notebook_id = store
    kit.kg_registers_the_occurrence_it_writes(
        sources, database, "%s", notebook_id, mutation,
    )


def test_knowledge_context_issues_one_read_over_admitted_objects_pg(store):
    kit.kg_one_read_per_call(*store[:2], "%s", store[2])


def test_collection_registers_only_the_citations_it_minted_pg(store):
    kit.collection_registers_only_what_it_minted(*store[:2], "%s", store[2])


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_follow_chain_hop_anchors_race_the_terminal_read_pg(store, mutation):
    sources, database, notebook_id = store
    kit.chain_race(sources, database, "%s", notebook_id, mutation)


def test_follow_chain_dangling_primary_is_not_minted_pg(store):
    kit.chain_dangling_pointer_is_not_minted(*store[:2], "%s", store[2])


def test_follow_chain_registration_never_mutates_its_input_pg(store):
    kit.chain_registration_never_mutates_its_input(*store[:2], "%s", store[2])


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_enumerated_row_citations_race_the_terminal_read_pg(store, mutation):
    sources, database, notebook_id = store
    kit.collection_race(sources, database, "%s", notebook_id, mutation)


def test_enumerated_dangling_element_row_is_not_minted_pg(store):
    kit.collection_dangling_element_row(*store[:2], "%s", store[2])


def test_enumerated_row_registered_then_deleted_is_minted_and_source_gone_pg(store):
    kit.collection_registered_row_deleted_before_minting(*store[:2], "%s", store[2])


def test_kg_element_registered_then_deleted_keeps_card_and_anchor_pg(store):
    kit.kg_registered_element_deleted_before_the_pointer_read(*store[:2], "%s", store[2])


def test_follow_chain_element_registered_then_deleted_keeps_its_locator_pg(store):
    kit.chain_registered_element_deleted_before_the_pointer_read(*store[:2], "%s", store[2])
