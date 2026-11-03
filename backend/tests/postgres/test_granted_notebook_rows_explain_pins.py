"""Plan pin of the notebook list's group/named partition on PostgreSQL
(share-edit P1-T7).

``granted_notebook_rows`` runs on every notebook-list open: a group branch
(``group_members`` of the caller -> the group grants of those groups) ``UNION
ALL`` a named branch (the caller's own ``principal_type='user'`` grants). On
2000 groups, 2000 group grants and 2000 named grants (``ANALYZE``d), for the
list and the point form, custom and generic plans, the statement the store
really issues (recorded, not rebuilt here) must:

* in the list form, start the group branch from ``idx_group_members_user``
  (cost bounded by the caller's memberships, not by the deployment's shares)
  and reach the grants of both branches through ``idx_notebook_grants_principal``
  (an Index or Bitmap Index Scan); the point form may instead start from the
  notebook's own grants (``uq_notebook_grants_principal``, a one-notebook probe);
* never ``Seq Scan`` ``notebook_grants`` or ``group_members``, even with
  ``enable_seqscan=off`` (the capability criterion: a sequential scan that
  survives it means no usable index path at all).
"""
from __future__ import annotations

import re

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.query_store import QueryStore
from tests.postgres.test_mount_sql_viewer_pg import _plan, analyze_and_settle

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_granted_rows_explain"),
]

_SEQ_SCAN = re.compile(r"Seq Scan on (notebook_grants|group_members)\b")
_SCALE = 2000


def _seed(execute, now) -> None:
    statements = [
        ("INSERT INTO users (id,email,display_name,username,password_hash,role,"
         "created_at,updated_at) SELECT 'u'||g, 'u'||g||'@t', 'u'||g, 'u'||g, 'x', "
         "'user', %s, %s FROM generate_series(1, %s) g", (now, now, _SCALE)),
        ("INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
         "SELECT 'g'||g, 'g'||g, 'project', '', 'u1', %s, %s "
         "FROM generate_series(1, %s) g", (now, now, _SCALE)),
        # every group has a few members; the probe user u7 is in exactly two
        ("INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
         "SELECT 'g'||g, 'u'||(1 + abs(hashtext('m'||g||'-'||k)) %% %s), "
         "CASE WHEN k = 1 THEN 'admin' ELSE 'member' END, %s, 'u1' "
         "FROM generate_series(1, %s) g CROSS JOIN generate_series(1, 4) k "
         "ON CONFLICT DO NOTHING", (_SCALE, now, _SCALE)),
        ("DELETE FROM group_members WHERE user_id='u7'", ()),
        ("INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
         "VALUES ('g1','u7','member',%s,'u1'),('g2','u7','admin',%s,'u1')", (now, now)),
        ("INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
         "created_at,updated_at,tier) SELECT 'nb'||g, 'nb'||g, '', 'Semiconductor', "
         "'draft', 'u'||(1 + g %% %s), %s, %s, 'personal' "
         "FROM generate_series(1, %s) g", (_SCALE, now, now, 2 * _SCALE)),
        ("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
         "created_by,created_at) SELECT 'gg'||g, 'nb'||g, "
         "CASE WHEN g %% 5 = 0 THEN 'group_admins' ELSE 'group' END, 'g'||g, "
         "CASE WHEN g %% 5 = 0 THEN 'admin' ELSE 'viewer' END, 'u1', %s "
         "FROM generate_series(1, %s) g", (now, _SCALE)),
        ("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
         "created_by,created_at) SELECT 'gu'||g, 'nb'||(%s + g), 'user', "
         "'u'||(1 + abs(hashtext('n'||g)) %% %s), "
         "CASE WHEN g %% 3 = 0 THEN 'editor' ELSE 'viewer' END, 'u1', %s "
         "FROM generate_series(1, %s) g ON CONFLICT DO NOTHING",
         (_SCALE, _SCALE, now, _SCALE)),
        ("INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,"
         "created_by,created_at) VALUES ('gu-probe','nb3999','user','u7','editor','u1',%s) "
         "ON CONFLICT DO NOTHING", (now,)),
    ]
    for sql, params in statements:
        execute(sql, params)


@pytest.fixture
def pg_partition_world(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        _seed(db.execute, now)
    analyze_and_settle(
        postgres_database.settings.database_url,
        ("notebooks", "notebook_grants", "group_members", "groups", "users"),
        held=False,
    )
    return postgres_database


class _Capture:
    sql = ""
    params: tuple = ()

    def execute(self, sql, params=()):
        self.sql, self.params = sql, tuple(params)
        return self

    @staticmethod
    def fetchall():
        return []


def _recorded(notebook_id=None):
    capture = _Capture()
    QueryStore.granted_notebook_rows(capture, "u7", notebook_id)
    return capture.sql, capture.params


def test_granted_rows_branches_keep_their_index_paths(pg_partition_world):
    database = pg_partition_world
    with database.connect() as connection:
        rows = QueryStore.granted_notebook_rows(connection, "u7")
        assert {row["_grant_principal_type"] for row in rows} >= {"user"}
        for label, notebook_id in (("list", None), ("point", "nb3999")):
            sql, params = _recorded(notebook_id)
            assert "UNION ALL" in sql
            for mode in ("custom", "generic"):
                plan = _plan(connection, sql, params, mode=mode)
                assert not _SEQ_SCAN.search(plan), f"{label}/{mode}:\n{plan}"
                if notebook_id is None:
                    assert "idx_group_members_user" in plan, f"{label}/{mode}:\n{plan}"
                    assert plan.count("idx_notebook_grants_principal") >= 2, (
                        f"{label}/{mode}: both branches probe the principal index:\n{plan}"
                    )
                    assert re.search(
                        r"(Index|Bitmap Index) Scan (using|on) "
                        r"idx_notebook_grants_principal", plan,
                    ), plan
            capability = _plan(connection, sql, params, seqscan=False)
            assert not _SEQ_SCAN.search(capability), (
                f"{label} (enable_seqscan=off):\n{capability}"
            )
