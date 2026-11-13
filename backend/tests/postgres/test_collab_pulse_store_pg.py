import pytest

from tests.collab_pulse_store_cases import CASES
from tests.notebook_edit_access_cases import build_world

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository
    postgres_settings.storage_dir = str(tmp_path / "storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_collab_store(repo, case):
    CASES[case](build_world(repo, postgres=True))


@pytest.mark.postgres_fresh_schema
def test_notebook_activity_migration_fresh_equals_upgraded(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    def shape():
        with postgres_database.connect() as db:
            return {
                "columns": [dict(row) for row in db.execute(
                    "SELECT column_name,data_type,is_nullable,column_default,collation_name "
                    "FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='notebook_activity' ORDER BY ordinal_position")],
                "constraints": [dict(row) for row in db.execute(
                    "SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                    "WHERE conrelid='notebook_activity'::regclass ORDER BY conname")],
                "indexes": [dict(row) for row in db.execute(
                    "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                    "AND tablename='notebook_activity' ORDER BY indexname")],
            }

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=88) == 88
    fresh = shape()
    with postgres_database.write() as db:
        db.execute("DROP TABLE notebook_activity")
        db.execute("DELETE FROM silicon_schema_migrations WHERE version=88")
    assert migrator.migrate(target_version=88) == 88
    assert migrator.migrate(target_version=88) == 88
    assert shape() == fresh
    assert len(fresh["columns"]) == 8
    foreign_keys = [row["definition"] for row in fresh["constraints"] if "FOREIGN KEY" in row["definition"]]
    assert len(foreign_keys) == 1
    assert "REFERENCES notebooks(id) ON DELETE CASCADE" in foreign_keys[0]
