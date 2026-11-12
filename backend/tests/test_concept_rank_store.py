"""SQLite concept-rank semantics and production/no-statistics plans."""
import re
import sqlite3

import pytest

from tests import concept_rank_store_cases as cases
from tests.test_kg_findings_store import _PRODUCTION_STATS
from tests.test_kg_findings_store import repo as repo


@pytest.mark.parametrize("check", [
    cases.check_counts, cases.check_names_citations, cases.check_publication, cases.check_deletion,
])
def test_concept_rank_contract(repo, check):
    check(repo, postgres=False)


def test_representatives_materialize_only_one_id_per_canonical(repo):
    nb = cases.seed(repo, postgres=False)
    batches = []

    class ObservedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            batches.append(rows)
            return rows

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters):
            return ObservedCursor(self.connection.execute(sql, parameters))

    with repo._runtime.unified_kg.read_snapshot() as db:
        assert repo._runtime.unified_kg.concept_rank_representatives(
            ObservedConnection(db), nb, ["wide", "single", "missing"],
        ) == {"wide": "wide-00", "single": "single"}
    assert len(batches) == 1
    # The cluster has fifteen members; only one scalar id crosses the SQL
    # boundary per requested canonical, with no name or evidence hydration.
    assert len(batches[0]) == 3
    assert all(set(row.keys()) == {"can", "object_id"} for row in batches[0])


@pytest.mark.parametrize("statistics", ["none", "production"])
def test_rank_member_reads_use_cluster_and_object_keys(repo, monkeypatch, statistics):
    from app.repositories.sqlite import database as sqlite_database

    nb = cases.seed(repo, postgres=False)
    rt = repo._runtime
    if statistics == "production":
        with rt.database.write() as db:
            db.execute("ANALYZE")
            for table in sorted({t for t, _, _ in _PRODUCTION_STATS}):
                db.execute("DELETE FROM sqlite_stat1 WHERE tbl=?", (table,))
            db.executemany("INSERT INTO sqlite_stat1(tbl,idx,stat) VALUES (?,?,?)", _PRODUCTION_STATS)
        rt.database.close_local()
    captured = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if "json_each(?)" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    with rt.unified_kg.read_snapshot() as db:
        assert rt.unified_kg.concept_recount_rows(db, nb, ["wide", "single"], ceiling=None)
        assert rt.unified_kg.concept_rank_names(
            db, nb, ["wide", "single"], ceiling=None, scan=10)
        assert rt.unified_kg.concept_rank_citation_rows(
            db, nb, ["wide", "single"], ceiling=None, scan=10)
        assert rt.unified_kg.concept_rank_representatives(
            db, nb, ["wide", "single"]) == {"wide": "wide-00", "single": "single"}
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 4
    with rt.database.connect() as db:
        for sql, params in captured:
            plan = "\n".join(row[3] for row in sqlite3.Connection.execute(
                db, "EXPLAIN QUERY PLAN " + sql, params))
            assert "idx_clusters_nb_canonical_member_gen (notebook_id=? AND canonical_id=?)" in plan, plan
            assert len(re.findall(
                r"SEARCH o (?:EXISTS )?USING INDEX sqlite_autoindex_knowledge_objects_1 \(id=\?\)",
                plan)) == 2, plan
            assert not re.search(r"SCAN (?:o|cc|c|c2)\b", plan), plan
            if "AS object_id FROM json_each(?) k" in sql:
                assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan
