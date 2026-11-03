import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.collab_pulse_store_cases import CASES
from tests.notebook_edit_access_cases import build_world


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path / 'pulse.db'}",
                                           storage_dir=str(tmp_path / "storage"),
                                           event_log_enabled=False, llm_log_enabled=False))
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_collab_store(repo, case):
    CASES[case](build_world(repo, postgres=False))


def test_owner_projection_and_pulse_plans_are_indexed(repo):
    world = build_world(repo, postgres=False)
    sql = world.access_sql
    with repo._runtime.database.connect() as db:
        for user in world.users.values():
            for boolean, owner, params in (
                (sql.NOTEBOOK_EDIT_SQL, sql.NOTEBOOK_EDIT_OWNER_SQL, sql.edit_access_params(user)),
                (sql.NOTEBOOK_ADMIN_SQL, sql.NOTEBOOK_ADMIN_OWNER_SQL, sql.admin_access_params(user)),
            ):
                plans = [list(db.execute("EXPLAIN QUERY PLAN " + query, (world.notebook, *params)))
                         for query in (boolean, owner)]
                assert [tuple(row) for row in plans[0]] == [tuple(row) for row in plans[1]]
            plan = list(db.execute("EXPLAIN QUERY PLAN " + sql.NOTEBOOK_COLLAB_PULSE_WANTED_SQL,
                                   sql.collab_pulse_wanted_params(world.notebook, user)))
            assert not any("SCAN " in row[3] for row in plan), [tuple(row) for row in plan]
