"""M3 store layer (PostgreSQL, 真实表): the participant set follows the viewer.

SQLite twin: ``tests/test_mount_viewer_store.py``.  World, expectations and the
seven per-call-site checks are ``tests/mount_viewer_store_cases.py`` -- the same
data on both backends, only the dialect differs (``%s`` binds, ``COLLATE "C"``
columns compared with the viewer value, ``CAST(%s AS text)`` viewer row).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.notebook_store import NotebookStore
from app.repositories.postgres.query_store import QueryStore
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from tests import mount_viewer_store_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mount_viewer_store"),
]

_NOW = normalize_timestamp("2026-01-01T00:00:00+00:00")


@pytest.fixture
def pg_store_world(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        cases.seed_store_world(db.execute, "%s", _NOW)
    database = postgres_database
    notebooks = NotebookStore(
        database,
        new_id=lambda prefix: f"{prefix}-x",
        now=lambda: _NOW,
        activity_retention_days=30,
    )
    unified = UnifiedKgStore(database, now=lambda: _NOW)
    knowledge = KnowledgeStore(database, seams=None)

    def on_db(call):
        def run(*args, **kwargs):
            with database.connect() as connection:
                return call(connection, *args, **kwargs)

        return run

    stores = SimpleNamespace(
        resolve_participants=on_db(
            lambda db, nb, v: NotebookStore.resolve_participants(db, nb, viewer_id=v)
        ),
        participant_ids=on_db(
            lambda db, nb, v: NotebookStore.participant_ids(db, nb, viewer_id=v)
        ),
        participant_tiers=on_db(
            lambda db, nb, v: NotebookStore.participant_tiers(db, nb, viewer_id=v)
        ),
        participant_notebook_ids=lambda nb, v: notebooks.participant_notebook_ids(
            nb, viewer_id=v
        ),
        participant_rows=on_db(
            lambda db, nb, v: NotebookStore.participant_rows(db, nb, viewer_id=v)
        ),
        usable_base_kg=on_db(
            lambda db, nb, v: QueryStore.notebook_has_usable_base_kg(db, nb, viewer_id=v)
        ),
        mounted_bases_row=on_db(
            lambda db, nb, v: QueryStore.mounted_bases_row(db, nb, viewer_id=v)
        ),
        mounted_base_ids=lambda nb, v: unified.mounted_base_ids(nb, viewer_id=v),
        any_mounted_has_kg_on=on_db(
            lambda db, nb, v: KnowledgeStore.any_mounted_has_kg_on(db, nb, viewer_id=v)
        ),
        any_mounted_has_kg=lambda nb, v: knowledge.any_mounted_has_kg(nb, viewer_id=v),
        any_mounted_has_kg_compat=lambda nb, v: knowledge.any_mounted_has_kg_compat(
            nb, viewer_id=v
        ),
        follow_start=on_db(
            lambda db, object_id, nb, v: KnowledgeStore.follow_start_row(
                db, object_id, nb, USABLE_STATUSES, viewer_id=v
            )
        ),
    )
    raw_calls = SimpleNamespace(
        resolve_participants=on_db(lambda db, nb: NotebookStore.resolve_participants(db, nb)),
        participant_ids=on_db(lambda db, nb: NotebookStore.participant_ids(db, nb)),
        participant_tiers=on_db(lambda db, nb: NotebookStore.participant_tiers(db, nb)),
        participant_notebook_ids=lambda nb: notebooks.participant_notebook_ids(nb),
        participant_rows=on_db(lambda db, nb: NotebookStore.participant_rows(db, nb)),
        notebook_has_usable_base_kg=on_db(
            lambda db, nb: QueryStore.notebook_has_usable_base_kg(db, nb)
        ),
        mounted_bases_row=on_db(lambda db, nb: QueryStore.mounted_bases_row(db, nb)),
        mounted_base_ids=lambda nb: unified.mounted_base_ids(nb),
        any_mounted_has_kg_on=on_db(
            lambda db, nb: KnowledgeStore.any_mounted_has_kg_on(db, nb)
        ),
        any_mounted_has_kg=lambda nb: knowledge.any_mounted_has_kg(nb),
        any_mounted_has_kg_compat=lambda nb: knowledge.any_mounted_has_kg_compat(nb),
        follow_start_row=on_db(
            lambda db, nb: KnowledgeStore.follow_start_row(db, "ko-b", nb, USABLE_STATUSES)
        ),
    )
    return SimpleNamespace(stores=stores, raw=raw_calls, database=database)


@pytest.mark.parametrize("site", sorted(cases.SITE_CHECKS))
def test_postgres_each_participant_call_site_follows_the_viewer(pg_store_world, site):
    assert cases.SITE_CHECKS[site](pg_store_world.stores) == []


def test_postgres_every_call_site_requires_the_viewer_keyword(pg_store_world):
    assert cases.required_keyword_failures(pg_store_world.raw) == []
