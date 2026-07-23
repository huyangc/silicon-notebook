from __future__ import annotations

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.ask import AskRequest
from app.models.notebooks import NotebookCreate
from app.models.sources import SourceImportFile, SourceImportRequest
from app.repositories.bundle import PersistenceBundle


pytestmark = pytest.mark.postgres_integration


def test_postgres_repository_boots_empty_schema_and_wires_complete_bundle(
    postgres_settings,
):
    from app.repositories.postgres.repository import PostgresRepository
    from app.repositories.factory import create_repository

    repository = create_repository(postgres_settings)
    assert isinstance(repository, PostgresRepository)
    try:
        runtime = repository._runtime
        assert runtime.database.__class__.__module__.startswith(
            "app.repositories.postgres."
        )
        for store in (
            runtime.identity,
            runtime.notebook_store,
            runtime.sharing_store,
            runtime.source_store,
            runtime.chunk_store,
            runtime.embedding_store,
            runtime.knowledge,
            runtime.governance,
            runtime.index_projections,
            runtime.kg_build_jobs,
            runtime.knowhow_store,
            runtime.knowhow_transfer_store,
            runtime.memory_store,
            runtime.queries,
            runtime.report_store,
            runtime.ask_state,
            runtime.unified_kg,
        ):
            assert store.__class__.__module__.startswith("app.repositories.postgres.")
        with repository._runtime.database.connect() as connection:
            assert connection.execute("SELECT count(*) AS n FROM notebooks").fetchone()[
                "n"
            ] == 0
            assert connection.execute(
                "SELECT count(*) AS n FROM users WHERE id='user-local' AND role='admin'"
            ).fetchone()["n"] == 1
            assert connection.execute("SELECT count(*) AS n FROM users").fetchone()[
                "n"
            ] == 1
    finally:
        repository.close()

    with pytest.raises(Exception):
        with repository._runtime.database.connect():
            pass

    # Keep the runtime protocol assertion close to the real boot. It catches a
    # missing store before a route first touches it in production.
    assert repository._runtime.database._closed is True


def test_postgres_bundle_factory_returns_the_backend_neutral_protocol(
    postgres_settings,
):
    from pathlib import Path

    from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    factory = PostgresPersistenceBundleFactory()
    bundle = factory.create(
        settings=postgres_settings,
        root_dir=Path(__file__).resolve().parents[3],
        seams=RepositoryCompatibilitySeams(
            new_id=lambda prefix: f"{prefix}-test",
            now=lambda: "2026-07-23T00:00:00+00:00",
            copy_chunk_size=lambda: 1000,
            remap_json_ids=lambda value, _maps: value,
            in_chunk_size=lambda: 900,
        ),
        model_config_cache={},
    )
    try:
        assert isinstance(bundle, PersistenceBundle)
    finally:
        bundle.database.close()


def test_postgres_bundle_closes_pool_when_migration_fails(
    postgres_settings,
    monkeypatch,
):
    from pathlib import Path

    from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    monkeypatch.setattr(
        PostgresMigrator,
        "migrate",
        lambda _self: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    factory = PostgresPersistenceBundleFactory()
    with pytest.raises(RuntimeError, match="migration failed"):
        factory.create(
            settings=postgres_settings,
            root_dir=Path(__file__).resolve().parents[3],
            seams=RepositoryCompatibilitySeams(
                new_id=lambda prefix: f"{prefix}-test",
                now=lambda: "2026-07-23T00:00:00+00:00",
                copy_chunk_size=lambda: 1000,
                remap_json_ids=lambda value, _maps: value,
                in_chunk_size=lambda: 900,
            ),
            model_config_cache={},
        )
    assert factory._database is not None
    assert factory._database._closed is True


def test_complete_postgres_repository_smoke_from_empty_schema(
    postgres_settings,
    tmp_path,
):
    """Exercise the production facade, not individual stores, on a fresh PG schema."""
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.knowhow.api import build_projector

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.kg_auto_extract = False
    postgres_settings.notebook_copy_max_rows = 0
    repository = PostgresRepository(postgres_settings)
    owner = repository.create_user("a00123456", "pw123456")
    reader = repository.create_user("b00654321", "pw123456")
    assert repository.authenticate_user("A00123456", "pw123456").id == owner.id
    session = repository.create_session(owner.id)
    assert repository.resolve_session(session).id == owner.id

    owner_token = set_request_user(owner)
    try:
        notebook = repository.create_notebook(
            NotebookCreate(name="PostgreSQL boot smoke")
        )
        imported = repository.import_sources(
            notebook.id,
            SourceImportRequest(
                files=[
                    SourceImportFile(
                        file_name="boot-evidence.md",
                        file_size=24,
                        mime_type="text/markdown",
                    )
                ]
            ),
        )
        assert len(imported) == 1
        assert repository.search_notebook(notebook.id, "boot-evidence").hits
        warm_progress = []
        assert repository.warm_open_path_caches(
            lambda done, total: warm_progress.append((done, total))
        ) == 1
        assert warm_progress == [(1, 1)]

        answer = repository.ask_chunk(
            notebook.id,
            AskRequest(question="What evidence is available?", mode="chunk"),
        )
        assert answer.answer_id
        assert answer.llm_mode == "deterministic"

        repository._runtime.memory_service.embedding_scheduler = (
            lambda function, item: function(item)
        )
        memory = repository.create_memory_from_answer(
            notebook.id,
            owner.id,
            answer.answer_id,
            "Boot memory",
            answer.answer or answer.conclusion,
            ["boot"],
            extract_kg=False,
        )
        assert memory.status == "confirmed"
        assert repository.get_memory(memory.id, owner.id).id == memory.id

        table_id = repository.create_knowhow_table(
            notebook.id,
            "Boot knowhow",
            "projection smoke",
            [
                {"name": "Topic", "role": "anchor"},
                {"name": "Procedure", "role": "procedure"},
            ],
            created_by=owner.id,
        )
        table = repository.get_knowhow_table(table_id)
        column_ids = {column["name"]: column["id"] for column in table["columns"]}
        repository.add_knowhow_row(
            table_id,
            {
                column_ids["Topic"]: "Power integrity",
                column_ids["Procedure"]: "Check the rail before signoff.",
            },
        )
        assert build_projector(repository).project_table(table_id, embed=False) == notebook.id
        projected = repository.get_knowhow_table(table_id)
        assert projected["hidden_source_id"]
        assert {row["projection_status"] for row in projected["rows"]} == {"synced"}
        with repository._runtime.database.connect() as connection:
            assert connection.execute(
                "SELECT count(*) AS n FROM knowledge_objects WHERE source_id=%s",
                (projected["hidden_source_id"],),
            ).fetchone()["n"] == 2

        report_id = repository.create_report(notebook.id, "Boot report", depth=1)
        repository.update_report(
            notebook.id,
            report_id,
            status="done",
            progress="complete",
            content_md="# PostgreSQL report",
        )
        assert repository.get_report(notebook.id, report_id)["status"] == "done"
        assert [item["id"] for item in repository.list_reports(notebook.id)] == [
            report_id
        ]

        share = repository.share_notebook(notebook.id)
        assert repository.find_notebook_by_share_token(share["share_token"]) == notebook.id
        joined = repository.join_shared(notebook.id, reader.id)
        assert joined.access == "reader"
        assert repository.user_can_read_notebook(notebook.id, reader.id)

        repository.delete_report(notebook.id, report_id)
        repository.delete_notebook(notebook.id)
        with pytest.raises(KeyError):
            repository.get_notebook(notebook.id)
        with repository._runtime.database.connect() as connection:
            for table_name in (
                "sources",
                "memory_items",
                "knowhow_tables",
                "reports",
                "notebook_members",
            ):
                assert connection.execute(
                    f"SELECT count(*) AS n FROM {table_name} WHERE notebook_id=%s",
                    (notebook.id,),
                ).fetchone()["n"] == 0
    finally:
        reset_request_user(owner_token)
        repository.close()
