"""Leaving a group: what the leaver stops seeing, on PostgreSQL.

Runs the exact scenarios of ``tests/test_group_leave_preview.py`` (defined once
in ``tests/group_leave_preview_cases.py``) against the production PostgreSQL
repository, plus the access predicate's equivalence with a real removal.
"""
from __future__ import annotations

import pytest

from app.repositories.postgres import access_sql
from tests.group_leave_preview_cases import CASES, build_world
from tests.memory_purge_cases import EMBED_DIM

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_group_leave_preview(repo, case):
    CASES[case](build_world(repo, postgres=True))


def test_after_group_exit_clause_matches_a_real_removal(repo):
    """Every group membership row of the world, every notebook: the clause
    evaluated before the removal equals the read check after the row is
    really deleted (inside a transaction that is rolled back)."""
    world = build_world(repo, postgres=True)
    clause = access_sql.read_access_after_group_exit_clause(
        "nb", "xnm", user_ref="gm.user_id", group_ref="gm.group_id",
        grant_alias="xg", group_alias="xgm", group_admin_alias="xga",
    )
    probe = (
        "SELECT 1 FROM group_members gm JOIN notebooks nb ON nb.id=%s "
        f"WHERE gm.group_id=%s AND gm.user_id=%s AND {clause}"
    )
    read = "SELECT 1 FROM notebooks nb WHERE nb.id=%s AND " + access_sql.read_access_clause()
    notebooks = sorted(world.notebooks.values())
    with repo._runtime.database.connect() as db:
        memberships = [
            (row["group_id"], row["user_id"])
            for row in db.execute("SELECT group_id,user_id FROM group_members").fetchall()
        ]
    assert len(memberships) >= 5
    flips = 0
    for group_id, user_id in memberships:
        with repo._runtime.database.connect() as db:
            predicted = {
                nb: db.execute(probe, (nb, group_id, user_id)).fetchone() is not None
                for nb in notebooks
            }
            before = {
                nb: db.execute(
                    read, (nb, *access_sql.read_access_params(user_id))
                ).fetchone() is not None
                for nb in notebooks
            }
        with repo._runtime.database.connect() as db:
            with db.transaction(force_rollback=True):
                db.execute(
                    "DELETE FROM group_members WHERE group_id=%s AND user_id=%s",
                    (group_id, user_id),
                )
                actual = {
                    nb: db.execute(
                        read, (nb, *access_sql.read_access_params(user_id))
                    ).fetchone() is not None
                    for nb in notebooks
                }
        assert predicted == actual, (group_id, user_id)
        flips += sum(1 for nb in notebooks if before[nb] and not actual[nb])
    assert flips >= 2
    # The rollback left every membership in place.
    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) AS c FROM group_members").fetchone()["c"] == len(
            memberships
        )
