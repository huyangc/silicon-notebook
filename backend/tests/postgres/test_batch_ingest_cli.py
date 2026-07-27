from __future__ import annotations

import json

import pytest

from app.repositories.postgres.repository import PostgresRepository
from app.services import batch_ingest as bi
from app.services import maintenance_cli
from app.services.embedding import FakeEmbedder
from tests.model_testkit import RecordingModelProvider


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_offline_maintenance"),
]


class _KgChat:
    configured = True
    model = "test-kg"

    def chat_json(self, messages, response_schema_hint, **_kwargs):
        return json.dumps({
            "nodes": [
                {
                    "local_id": "concept-1",
                    "type": "Concept",
                    "name": "PostgreSQL batch ingestion",
                    "ev": 0,
                }
            ],
            "edges": [],
        })


class _PaperChat:
    configured = True
    model = "test-paper"

    def chat_json(self, messages, response_schema_hint, **_kwargs):
        return json.dumps({"is_paper": False})


def _provider() -> RecordingModelProvider:
    embedder = FakeEmbedder(dim=16)
    workloads = (
        "retrieval_query_embedding",
        "source_element_embedding",
        "chunk_embedding",
        "knowledge_object_embedding",
        "relation_embedding",
        "memory_embedding",
        "knowhow_embedding",
    )
    return RecordingModelProvider(
        chat_clients={"kg_extract": _KgChat(), "paper_metadata": _PaperChat()},
        embedding_clients={workload: embedder for workload in workloads},
        parallelism_by_workload={workload: 2 for workload in workloads},
    )


def test_confirmed_postgres_cli_runs_portable_phases_without_sqlite(
    postgres_settings, tmp_path, monkeypatch, capsys
):
    """Exercise the real PG facade behind the same public CLI composition root."""
    settings = postgres_settings.model_copy(
        update={
            "storage_dir": str(tmp_path / "storage"),
            "model_services_config": "",
            "embed_dim": 16,
        }
    )
    opened_backends: list[str] = []

    def _open(_settings):
        repository = PostgresRepository(settings, model_provider=_provider())
        opened_backends.append(type(repository).__name__)
        return repository

    monkeypatch.setattr(maintenance_cli, "create_repository", _open)
    monkeypatch.setattr(bi, "Settings", lambda: settings)

    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    (source_dir / "one.md").write_text(
        "# PostgreSQL batch ingestion\n\nPostgreSQL batch ingestion is restartable.",
        encoding="utf-8",
    )
    common = ["--confirm-service-stopped"]
    assert bi.main([
        "ingest", "--input-dir", str(source_dir), "--notebook-name", "PG CLI",
        "--workers", "1", *common,
    ]) == 0

    probe = PostgresRepository(settings, model_provider=_provider())
    try:
        notebook_id = next(
            notebook.id for notebook in probe.list_notebooks() if notebook.name == "PG CLI"
        )
        wrong_owner = probe.create_user("z00123456", "pw123456")
    finally:
        probe.close()

    assert bi.main([
        "reparse", "--notebook-id", notebook_id, "--owner", wrong_owner.username,
        "--no-rebuild", *common,
    ]) == 2
    assert "notebook owner mismatch" in capsys.readouterr().err

    # No --limit: exercise the durable full-build path, whose source inventory
    # and submitted worker windows are bounded on PostgreSQL too.
    assert bi.main(["kg", "--notebook-id", notebook_id, *common]) == 0
    assert bi.main(["embed", "--notebook-id", notebook_id, *common]) == 0
    assert bi.main(["metadata", "--notebook-id", notebook_id, *common]) == 0
    assert bi.main([
        "reparse", "--notebook-id", notebook_id, "--no-rebuild", *common,
    ]) == 0
    assert bi.main([
        "backfill-source-index", "--notebook-id", notebook_id, *common,
    ]) == 0
    assert bi.main(["index", "--notebook-id", notebook_id, *common]) == 0

    verify = PostgresRepository(settings, model_provider=_provider())
    try:
        assert verify.maintenance.count_missing_chunk_vectors(notebook_id) == 0
        assert verify.maintenance.count_missing_element_vectors(notebook_id) == 0
        assert verify.maintenance.count_missing_node_vectors(notebook_id) == 0
        assert verify.maintenance.count_sources_missing_kg(notebook_id) == 0
        assert verify.maintenance.has_scale_index(notebook_id)
    finally:
        verify.close()
    assert opened_backends and set(opened_backends) == {"PostgresRepository"}
