"""PostgreSQL twin of ``tests/test_exact_lookup_ceiling_store.py``: the exact
lookup pushes the run's source ceiling into the store's probe."""
from __future__ import annotations

import pytest

from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients
from tests.test_exact_lookup_ceiling_store import (
    PgSeed, check_closed_channel, check_narrowed_window,
)

pytestmark = [pytest.mark.postgres_integration]


@pytest.fixture
def backend(postgres_settings):
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.notebooks import NotebookCreate
    from app.repositories.postgres.repository import PostgresRepository

    repo = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    bob = repo.create_user("b00654321", "password-12")
    token = set_request_user(bob)
    try:
        nb = repo.create_notebook(NotebookCreate(name="manual")).id
        yield repo, PgSeed(repo, nb), bob.id
    finally:
        reset_request_user(token)
        repo.close()


def test_pg_narrowed_window(backend):
    check_narrowed_window(backend)


def test_pg_closed_channel(backend):
    check_closed_channel(backend)
