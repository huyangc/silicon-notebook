"""PR-D P3-7 EXPLAIN pin (real PostgreSQL): the terminal citation check's
by-id visibility read (``SourceStore.visible_source_owners``).

The read replaced "every visible source id of each cited library" (about 49k
ids and 90 ms at the reviewer's scale) with a probe of exactly the sources an
answer cites. Its id list travels as ONE jsonb parameter, so the statement
text -- and therefore the prepared statement and any generic plan PostgreSQL
switches to after the fifth execution -- is the same whatever the list's
length. What is pinned, in the style of ``test_memory_sql_explain_pins.py``
(sequential scans switched off so a few thousand rows keep the question to
"which index path"):

* the custom plan and the GENERIC plan both unnest the list ONCE (an
  InitPlan) and probe ``sources`` through its primary key with it
  (``id = ANY(...)`` as the index condition);
* neither hashes a whole index against the list -- the shape an
  ``IN (SELECT jsonb_array_elements_text(...))`` semi-join took here, which
  reads every visible source again.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_global_citation_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "SELECT 'nb-'||g,'n',%s,%s FROM generate_series(0,7) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'src-'||g,'nb-'||(g%%8),'t',"
            "CASE WHEN g%%50=0 THEN 'memory' ELSE 'upload' END,%s,%s "
            "FROM generate_series(0,7999) g",
            (_NOW, _NOW),
        )
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) sources")


def _explain(connection, prefix: str, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    rows = connection.execute(f"EXPLAIN ({prefix}) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_by_id_visibility_read_probes_the_primary_key(postgres_database):
    from app.repositories.postgres.source_store import SourceStore
    from tests.citation_check_testkit import count_statements

    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    sources = object.__new__(SourceStore)
    sources.database = postgres_database
    ids = [f"src-{index * 331}" for index in range(24)]

    # The statement the store actually issues, explained as a custom and as a
    # generic plan.
    with count_statements(postgres_database) as statements:
        owners = sources.visible_source_owners([*ids, "src-missing"])
    [sql] = statements
    with postgres_database.connect() as connection:
        custom = _explain(connection, "COSTS OFF", sql, (json.dumps(ids),))
    with postgres_database.connect() as connection:
        generic = _explain(connection, "GENERIC_PLAN, COSTS OFF", sql.replace("%s", "$1"), ())
    for name, plan in (("custom", custom), ("generic", generic)):
        assert "Seq Scan on sources" not in plan, (name, plan)
        assert "Index Scan using pk_sources on sources" in plan, (name, plan)
        assert "Index Cond: (id = ANY (" in plan, (name, plan)
        assert "InitPlan" in plan, (name, plan)
        assert "Hash" not in plan, (name, plan)

    # src-0 is a Memory source (g % 50 == 0): hidden, so absent like a missing one.
    assert owners == {
        source_id: f"nb-{int(source_id.removeprefix('src-')) % 8}"
        for source_id in ids if int(source_id.removeprefix("src-")) % 50
    }
