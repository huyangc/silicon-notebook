"""PostgreSQL twin of the peer workbook lane's end-to-end case.

The SQLite half is ``tests/test_spreadsheet_federation.py::
test_quote_tables_of_two_libraries_federate_end_to_end``; the world and every
assertion are imported from it, so the two backends cannot drift.  What
differs is the store the federated seat reads through: the participant set,
the per-library visible-source read and the membership the viewer's access
rests on are all PostgreSQL here.
"""
from __future__ import annotations

import pytest

from tests.test_spreadsheet_federation import assert_quote_federation

pytestmark = pytest.mark.postgres_integration


def test_quote_tables_of_two_libraries_federate_on_postgres(postgres_settings, tmp_path):
    from app.services.embedding import FakeEmbedder
    from app.services.postgres_repository import PostgresRepository
    from tests.model_testkit import bind_all_embedding_clients

    postgres_settings.storage_dir = str(tmp_path / "pg-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    repository.settings.graph_ppr_enabled = False
    try:
        owner = repository._runtime.identity.create_user("o00000001", "pw123456")
        viewer = repository._runtime.identity.create_user("v00000003", "pw123456")
        assert_quote_federation(repository, tmp_path, owner, viewer)
    finally:
        repository.close()
