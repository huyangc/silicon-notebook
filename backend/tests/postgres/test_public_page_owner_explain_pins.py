"""E7-5 EXPLAIN pin (real PostgreSQL): the public report page's knowledge-object
ownership read (``KnowledgeStore.object_owners``, D-3).

A report citation of a mounted library's knowledge object may carry no
source; its library is then read by object id.  The read has the shape of the
pinned ``SourceStore.visible_source_owners``: the id list (bounded by the
report's citations) travels as ONE jsonb parameter, so the statement text --
and therefore the prepared statement and any generic plan PostgreSQL switches
to after the fifth execution -- is the same whatever the list's length.  What
is pinned (sequential scans switched off so a few thousand rows keep the
question to "which index path"):

* the custom plan and the GENERIC plan both unnest the list ONCE (an
  InitPlan) and probe ``knowledge_objects`` through its primary key with it;
* neither hashes a whole index against the list.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_public_page_owner_explain"),
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
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
            "created_at,updated_at) "
            "SELECT 'ko-'||g,'nb-'||(g%%8),'concept',"
            "CASE WHEN g%%40=0 THEN 'deprecated' ELSE 'approved' END,%s,%s "
            "FROM generate_series(0,7999) g",
            (_NOW, _NOW),
        )
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) knowledge_objects")


def _explain(connection, prefix: str, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    rows = connection.execute(f"EXPLAIN ({prefix}) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_object_ownership_read_probes_the_primary_key(postgres_database):
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from tests.citation_check_testkit import count_statements

    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    store = object.__new__(KnowledgeStore)
    store.database = postgres_database
    ids = [f"ko-{index * 331}" for index in range(24)]

    with count_statements(postgres_database) as statements:
        owners = store.object_owners([*ids, "ko-missing", ids[0]])
    [sql] = statements
    with postgres_database.connect() as connection:
        custom = _explain(connection, "COSTS OFF", sql, (json.dumps(ids),))
    with postgres_database.connect() as connection:
        generic = _explain(connection, "GENERIC_PLAN, COSTS OFF", sql.replace("%s", "$1"), ())
    for name, plan in (("custom", custom), ("generic", generic)):
        assert "Seq Scan on knowledge_objects" not in plan, (name, plan)
        assert "pk_knowledge_objects on knowledge_objects" in plan, (name, plan)
        assert "Index Cond: (id = ANY (" in plan, (name, plan)
        assert "InitPlan" in plan, (name, plan)
        assert "Hash" not in plan, (name, plan)

    # Ownership whatever the status (ko-0 is deprecated); an unknown id is absent.
    assert owners == {
        object_id: f"nb-{int(object_id.removeprefix('ko-')) % 8}" for object_id in ids
    }


def _seed_sources(postgres_database) -> None:
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
            "CASE WHEN g%%50=0 THEN 'memory' WHEN g%%50=1 THEN 'knowhow' "
            "ELSE 'upload' END,%s,%s FROM generate_series(0,7999) g",
            (_NOW, _NOW),
        )
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) sources")


def test_source_ownership_read_covers_hidden_projections_by_primary_key(postgres_database):
    """``SourceStore.source_owners``: ``visible_source_owners`` without its type
    predicate, so a cited Memory or Knowhow projection names its library too."""
    from app.repositories.postgres.source_store import SourceStore
    from tests.citation_check_testkit import count_statements

    assert PostgresMigrator(postgres_database).migrate()
    _seed_sources(postgres_database)
    store = object.__new__(SourceStore)
    store.database = postgres_database
    # src-0 / src-50 are Memory projections, src-1 / src-51 Knowhow ones.
    ids = ["src-0", "src-1", "src-50", "src-51", *(f"src-{i * 331}" for i in range(1, 20))]

    with count_statements(postgres_database) as statements:
        owners = store.source_owners([*ids, "src-missing", ids[0]])
    [sql] = statements
    with postgres_database.connect() as connection:
        custom = _explain(connection, "COSTS OFF", sql, (json.dumps(ids),))
    with postgres_database.connect() as connection:
        generic = _explain(connection, "GENERIC_PLAN, COSTS OFF", sql.replace("%s", "$1"), ())
    for name, plan in (("custom", custom), ("generic", generic)):
        assert "Seq Scan on sources" not in plan, (name, plan)
        assert "pk_sources on sources" in plan, (name, plan)
        assert "Index Cond: (id = ANY (" in plan, (name, plan)
        assert "InitPlan" in plan, (name, plan)
        assert "Hash" not in plan, (name, plan)

    assert owners == {
        source_id: f"nb-{int(source_id.removeprefix('src-')) % 8}" for source_id in ids
    }
