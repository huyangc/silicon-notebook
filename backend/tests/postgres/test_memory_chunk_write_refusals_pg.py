"""E4-1b on the real PostgreSQL backend: the KG build publish and the Knowhow
transfer insert refuse a Memory source's chunks, and the sync import refuses,
for the whole run and before any table is applied, a package that carries them
or that gives a source id a different type than the target with one of the two
types Memory. Mirrors
``tests/test_memory_chunk_write_refusals.py`` (SQLite) -- the scenario functions
are shared and backend-neutral; see that file's module docstring for the
contract.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import export_notebooks
from app.migration.sync.import_ import import_package
from tests.postgres.conftest import _isolated_postgres_scope
from tests.test_indexing_pipeline_chunking import _PipelineHost
from tests.test_memory_chunk_write_refusals import (
    ALICE,
    NOW,
    scenario_import_refuses_memory_sources,
    scenario_import_refuses_passages_of_a_target_side_memory_source,
    scenario_publish_refuses_memory_source,
    scenario_transfer_refuses_memory_source,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_chunk_write_refusals"),
]


def _settings(url: str, storage: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=url,
        storage_dir=str(storage),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def postgres_repository(postgres_scope, tmp_path, monkeypatch):
    from app.repositories.postgres.repository import PostgresRepository

    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = PostgresRepository(
        _settings(postgres_scope.url, tmp_path / "storage"),
        indexing_pipeline_host=_PipelineHost(),
    )
    try:
        yield repository
    finally:
        repository.close()


def test_publish_refuses_memory_source(postgres_repository, monkeypatch):
    from app.repositories.postgres import kg_build_job_store

    scenario_publish_refuses_memory_source(
        postgres_repository,
        lambda: monkeypatch.setattr(
            kg_build_job_store, "VISIBLE_SOURCE_TYPES_PREDICATE", "TRUE"
        ),
    )


def test_transfer_refuses_memory_source(postgres_repository):
    scenario_transfer_refuses_memory_source(postgres_repository)


def _add_user(repo, user_id: str, username: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES(%s,%s,%s,%s,'active',%s,%s,%s)",
            (user_id, f"{user_id}@example.invalid", username.title(), "user",
             username, NOW, NOW),
        )


def test_import_refuses_memory_sources(
    postgres_scope, tmp_path, monkeypatch
):
    from app.repositories.postgres.repository import PostgresRepository

    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    dst_settings = _settings(postgres_scope.url, tmp_path / "dst-storage")
    dst = PostgresRepository(dst_settings)
    try:
        with _isolated_postgres_scope(base_url) as src_scope:
            src_settings = _settings(src_scope.url, tmp_path / "src-storage")
            src = PostgresRepository(src_settings)
            try:
                _add_user(src, ALICE[0], ALICE[2])
                _add_user(dst, ALICE[1], ALICE[2])
                exports = iter(range(100))

                def export():
                    with src._connect() as db:
                        notebook = db.execute(
                            "SELECT id FROM notebooks WHERE created_by=%s", (ALICE[0],)
                        ).fetchone()["id"]
                    report = export_notebooks(
                        src_settings, target_env="prod",
                        out_dir=tmp_path / f"out{next(exports)}",
                        notebook_ids=[notebook], source_env="dev",
                    )
                    return report.package_dir

                scenario_import_refuses_memory_sources(
                    src, dst, export,
                    lambda pkg, **kw: import_package(dst_settings, pkg, **kw),
                    ALICE[0], monkeypatch,
                )
            finally:
                src.close()
    finally:
        dst.close()


def test_import_refuses_passages_of_a_target_side_memory_source(
    postgres_repository, postgres_scope, tmp_path, monkeypatch
):
    import app.migration.sync.import_ as import_module

    settings = _settings(postgres_scope.url, tmp_path / "storage")

    def check(context):
        backend = import_module._Backend(settings, Path(__file__).resolve().parents[3])
        try:
            with backend.read() as conn:
                import_module._preflight_memory_sources(backend, conn, context)
        finally:
            backend.close()

    scenario_import_refuses_passages_of_a_target_side_memory_source(
        postgres_repository, tmp_path, check, monkeypatch
    )
