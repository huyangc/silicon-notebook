"""The user-reference registry covers the migrated SQLite schema exactly.

The PostgreSQL half is ``tests/postgres/test_user_references_pg.py``.
"""
from app.core.config import Settings
from app.repositories.user_references import REFERENCE_LABEL_UNITS, USER_REFERENCE_COLUMNS
from app.services.sqlite_repository import SQLiteRepository
from tests.user_reference_guard import registry_violations


def test_every_user_reference_in_the_sqlite_schema_is_classified(tmp_path):
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/refs.db", storage_dir=str(tmp_path / "storage"),
        _env_file=None, event_log_enabled=False, llm_log_enabled=False,
    ))
    try:
        columns, foreign_keys = set(), set()
        with repo._runtime.identity.database.connect() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                columns |= {(table, info[1]) for info in db.execute(f"PRAGMA table_info('{table}')")}
                foreign_keys |= {
                    (table, fk[3]) for fk in db.execute(f"PRAGMA foreign_key_list('{table}')") if fk[2] == "users"
                }
    finally:
        repo.close()
    assert registry_violations(columns, foreign_keys) == []


def test_every_blocking_category_has_a_display_unit():
    labels = {ref.label for ref in USER_REFERENCE_COLUMNS if ref.kind == "blocking"}
    assert labels == set(REFERENCE_LABEL_UNITS)
    assert all(not ref.label for ref in USER_REFERENCE_COLUMNS if ref.kind != "blocking")
