import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.auth_store_contract import AuthStoreContract


@pytest.fixture
def identity(tmp_path):
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/auth.db",storage_dir=str(tmp_path/"storage"),_env_file=None,event_log_enabled=False,llm_log_enabled=False))
    yield repo._runtime.identity
    repo.close()


class TestSQLiteAuthStore(AuthStoreContract):
    pass


def _plan_of(identity, call):
    """Run ``call(db)`` on a connection that records every statement, then
    EXPLAIN QUERY PLAN each username lookup it issued."""
    seen = []
    with identity.database.connect() as db:
        db.set_trace_callback(seen.append)
        call(db)
        db.set_trace_callback(None)
        lookups = [sql for sql in seen if "lower(username)" in sql]
        assert lookups, seen
        return [
            "\n".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            for sql in lookups
        ]


def test_username_lookups_use_the_partial_unique_index(identity):
    """idx_users_username_lower is partial (WHERE username<>''); a lookup that
    omits that predicate cannot use it and falls back to SCAN users."""
    plans = _plan_of(identity, lambda db: identity.auth.local_account(db, "Someone"))
    plans += _plan_of(identity, lambda db: identity.auth.check_name(db, "Someone", "u-other"))
    for plan in plans:
        assert "idx_users_username_lower" in plan, plan
