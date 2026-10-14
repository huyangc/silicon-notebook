"""The user-reference registry covers the migrated PostgreSQL schema exactly.

The SQLite half is ``tests/test_user_references.py``.
"""
import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from tests.user_reference_guard import registry_violations


pytestmark = pytest.mark.postgres_integration


def test_every_user_reference_in_the_postgres_schema_is_classified(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.connect() as db:
        columns = {
            (r["table_name"], r["column_name"]) for r in db.execute(
                "SELECT c.table_name,c.column_name FROM information_schema.columns c "
                "JOIN information_schema.tables t ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
                "WHERE c.table_schema=current_schema() AND t.table_type='BASE TABLE'"
            ).fetchall()
        }
        foreign_keys = {
            (r["table_name"], r["column_name"]) for r in db.execute(
                "SELECT src.relname AS table_name,a.attname AS column_name "
                "FROM pg_constraint k "
                "JOIN pg_class src ON src.oid=k.conrelid "
                "JOIN pg_class dst ON dst.oid=k.confrelid "
                "JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=ANY(k.conkey) "
                "WHERE k.contype='f' AND dst.relname='users' "
                "AND src.relnamespace=current_schema()::regnamespace"
            ).fetchall()
        }
    assert columns
    assert registry_violations(columns, foreign_keys) == []
