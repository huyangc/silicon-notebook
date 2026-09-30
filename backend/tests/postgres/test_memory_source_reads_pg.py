"""E3-1 on PostgreSQL: the Memory owner gate of the source/element reads and the
retrieval ceiling's hidden half, on real tables, plus EXPLAIN pins.

Two statements changed in this task, both built from
`postgres/memory_sql.memory_source_readable` (one scalar parameter, the viewer):

* `sharing_store._VIEWER_SOURCE_NOTEBOOK_SQL` -- `source_notebook_id(source_id,
  viewer_id=...)`, the first read of `/sources/{id}`, its element endpoints, the
  participant-scope proxy and MCP `get_cited_element`. Another member's Memory,
  an orphaned Memory row and a missing id all answer None from this one
  statement; `viewer_id=None` keeps the historical ungated statement.
* `source_store._HIDDEN_SOURCE_IDS_SQL` -- `hidden_source_ids`, now consuming
  the same fragment instead of a hand-written twin.

The SQLite side of the same behaviour is pinned through the real routes by
`tests/test_memory_source_endpoints.py`.

EXPLAIN judgement follows `test_memory_sql_explain_pins.py`: seqscan and
bitmapscan off, assert the planner still has an index path (a rewrite without
one shows up as a Seq Scan). The gate is a primary-key probe on `sources` and,
only for a Memory row, a primary-key probe on `memory_items` -- no data-sized
bind anywhere (the viewer is the only extra parameter).
"""
from __future__ import annotations

import pytest

from app.repositories.postgres import memory_sql
from app.repositories.postgres import sharing_store as sharing_store_module
from app.repositories.postgres import source_store as source_store_module
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.sharing_store import SharingStore
from app.repositories.postgres.source_store import SourceStore

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_source_reads"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_TABLES = ("sources", "memory_items")


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for user in ("u-a", "u-b"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@example.test", user, _NOW, _NOW, user),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        # Even g: created by u-a; odd g: created by u-b.
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||g,'nb',CASE WHEN g%%2=0 THEN 'u-a' ELSE 'u-b' END,"
            "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(0,199) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||g,'nb','t',"
            "CASE WHEN g<200 THEN 'memory' WHEN g<400 THEN 'knowhow' ELSE 'upload' END,"
            "CASE WHEN g<200 THEN 'mem-'||g END,%s,%s FROM generate_series(0,3999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) VALUES ('src-orphan','nb','t','memory',NULL,%s,%s)",
            (_NOW, _NOW),
        )
        for table in _TABLES:
            db.execute(f"ANALYZE {table}")
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


@pytest.fixture
def stores(postgres_database, postgres_settings):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    sharing = SharingStore(
        postgres_database,
        postgres_settings,
        now=lambda: _NOW,
        insert_row=SharingStore.insert_row_values,
    )
    return postgres_database, sharing, SourceStore(postgres_database, now=lambda: _NOW)


def test_viewer_gated_notebook_lookup_matches_the_endpoint_matrix(stores):
    _database, sharing, _sources = stores
    lookup = sharing.source_notebook_id
    # Own Memory (src-0 → mem-0 → u-a); another member's (src-1 → u-b).
    assert lookup("src-0", viewer_id="u-a") == "nb"
    assert lookup("src-1", viewer_id="u-a") is None
    assert lookup("src-1", viewer_id="u-b") == "nb"
    assert lookup("src-0", viewer_id="u-b") is None
    # No viewer (a token without memory:read) reads no Memory at all.
    assert lookup("src-0", viewer_id="") is None
    # Orphaned Memory reads for nobody; a missing id answers the same None.
    assert lookup("src-orphan", viewer_id="u-a") is None
    assert lookup("src-does-not-exist", viewer_id="u-a") is None
    # Knowhow and ordinary sources are not gated.
    assert lookup("src-200", viewer_id="u-b") == "nb"
    assert lookup("src-500", viewer_id="") == "nb"
    # viewer_id omitted: the historical ungated lookup (write-side callers).
    assert lookup("src-1") == "nb"
    assert lookup("src-orphan") == "nb"
    assert lookup("src-does-not-exist") is None


def test_hidden_source_ids_consume_the_shared_fragment(stores):
    _database, _sharing, sources = stores
    assert memory_sql.memory_source_readable("s") in (
        source_store_module._HIDDEN_SOURCE_IDS_SQL
    )
    assert memory_sql.memory_source_readable("s") in (
        sharing_store_module._VIEWER_SOURCE_NOTEBOOK_SQL
    )
    hidden_a = sources.hidden_source_ids("nb", "u-a")
    memory_a = {value for value in hidden_a if int(value.split("-")[1]) < 200}
    assert memory_a == {f"src-{g}" for g in range(0, 200, 2)}
    assert len(hidden_a) == 100 + 200  # own Memory + every Knowhow row
    assert "src-orphan" not in hidden_a
    assert sources.hidden_source_ids("nb", "") == [
        f"src-{g}" for g in sorted(range(200, 400), key=lambda g: f"src-{g}")
    ]


def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_changed_statements_keep_their_index_paths(stores):
    database, _sharing, _sources = stores
    with database.connect() as connection:
        gate = _plan(
            connection, sharing_store_module._VIEWER_SOURCE_NOTEBOOK_SQL, ("src-1", "u-a")
        )
        hidden = _plan(
            connection, source_store_module._HIDDEN_SOURCE_IDS_SQL, ("nb", "u-a")
        )
    for name, plan in (("gate", gate), ("hidden", hidden)):
        assert "Seq Scan" not in plan, (name, plan)
    # The gate: one primary-key probe on sources; the Memory check a correlated
    # primary-key probe on memory_items for that single row (evaluated only
    # when the row IS a Memory source) -- nothing scales with the notebook.
    assert "Index Scan using pk_sources on sources s" in gate, gate
    assert "Index Scan using pk_memory_items on memory_items rm" in gate, gate
    assert "hashed SubPlan" not in gate, gate
    # The hidden half: the notebook's hidden-type index, the owner check a
    # once-evaluated hashed SubPlan on the owner index (same shape the E0 pin
    # records for the fragment on its own).
    assert "idx_sources_nb_hidden_type" in hidden, hidden
    assert "hashed SubPlan" in hidden, hidden
    assert "idx_memory_owner_notebook_status" in hidden, hidden
