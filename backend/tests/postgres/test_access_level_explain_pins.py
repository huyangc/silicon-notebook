"""Cost pin of the access-level lookup on PostgreSQL (share-edit P1-T1, PD-5).

``NOTEBOOK_ACCESS_LEVEL_SQL`` answers owner / admin / edit / read in one
statement; the body-level checks of the editor tier (deleting someone else's
source, the whole-table Knowhow delete, the pulse endpoint of P2) run it on
every such request. On the realistic access-control fixture of
``test_mount_sql_viewer_pg.py`` (thousands of notebooks, members, grants and
group memberships, now with editor and admin rows in every arm), at two scales
and for every kind of caller, the plan must reach ``notebook_grants``,
``notebook_members`` and ``group_members`` through an index: no ``Seq Scan``
on any of them in the custom or the generic plan, and none even with
``enable_seqscan=off`` (the capability criterion: a sequential scan that
survives it means the table has no usable index path at all).
"""
from __future__ import annotations

import re

import pytest

from app.repositories.postgres import access_sql
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from tests.postgres.test_mount_sql_viewer_pg import (
    _plan,
    analyze_and_settle,
    seed_realistic_mount_fixture,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_access_level_explain"),
]

_ACCESS_TABLES = ("notebooks", "notebook_members", "notebook_grants", "group_members")
_SEQ_SCAN = re.compile(r"Seq Scan on (notebook_grants|notebook_members|group_members)\b")

_PROBE = "nbP0"
# (label, caller): the owner, a link member, a named editor, a group admin via a
# group_admins admin edge, a legacy named admin, and a stranger.
_CALLERS = (
    ("owner", "u7"),
    ("member", "u500"),
    ("editor", "u600"),
    ("group_admin", "u601"),
    ("legacy_admin", "u602"),
    ("stranger", "u999"),
)
_EXPECTED_LEVEL = {
    "owner": "owner", "member": "read", "editor": "edit",
    "group_admin": "admin", "legacy_admin": "admin", "stranger": None,
}


def _seed_editor_rows(execute, now) -> None:
    """Give every arm of the CASE real rows: a share of the fixture's named
    grants become editor edges, a share of its group_admins edges become admin
    edges, plus the probe notebook's own callers."""
    statements = [
        (
            "UPDATE notebook_grants SET role='editor' "
            "WHERE principal_type='user' AND abs(hashtext(id)) %% 4 = 0",
            (),
        ),
        (
            "UPDATE notebook_grants SET role='admin' "
            "WHERE principal_type IN ('group_admins','group') "
            "AND abs(hashtext(id)) %% 5 = 0",
            (),
        ),
        # the probe's own group: no fixture member can reach nbP0 through it
        (
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES ('gP','gP','project','','u7',%s,%s)",
            (now, now),
        ),
        (
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES ('gP','u601','admin',%s,'u7')",
            (now,),
        ),
        (
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES ('gr-probe-editor','nbP0','user','u600','editor','u7',%s),"
            "('gr-probe-gadmins','nbP0','group_admins','gP','admin','u7',%s),"
            "('gr-probe-uadmin','nbP0','user','u602','admin','u7',%s)",
            (now, now, now),
        ),
    ]
    for sql, params in statements:
        execute(sql, params)


@pytest.fixture(params=(3000, 8000))
def pg_access_world(request, postgres_database):
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        seed_realistic_mount_fixture(db.execute, request.param, now)
        _seed_editor_rows(db.execute, now)
    analyze_and_settle(
        postgres_database.settings.database_url, _ACCESS_TABLES + ("users", "groups"),
        held=False,
    )
    return postgres_database, request.param


def test_access_level_has_no_seq_scan_on_access_tables(pg_access_world):
    database, notebooks = pg_access_world
    query = access_sql.NOTEBOOK_ACCESS_LEVEL_SQL
    with database.connect() as connection:
        for label, caller in _CALLERS:
            params = access_sql.access_level_params(_PROBE, caller)
            row = connection.execute(query, params).fetchone()
            assert row is not None and row["level"] == _EXPECTED_LEVEL[label], (
                label, dict(row or {}),
            )
            assert row["owner_id"] == "u7"
            for mode in ("custom", "generic"):
                plan = _plan(connection, query, params, mode=mode)
                assert not _SEQ_SCAN.search(plan), f"{notebooks} / {mode} / {label}:\n{plan}"
            capability = _plan(connection, query, params, seqscan=False)
            assert not _SEQ_SCAN.search(capability), (
                f"{notebooks} / {label} (enable_seqscan=off):\n{capability}"
            )
