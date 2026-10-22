"""EXPLAIN pins for the statements that read ``kg_cluster_scratch`` /
``kg_canonical_scratch`` during a rebuild (PostgreSQL).

Every rebuild writes its scratch rows under a NEW run_id, so the planner sees
the two scratch tables either with no statistics (never analyzed) or with the
statistics of an earlier run (``run_id``'s MCV is that one old value). In both
states it estimates one row per side. Before the fix the cluster-map write
joined the two tables with a plain JOIN, and under that estimate the planner
chose a Nested Loop whose inner side scanned the canonical scratch by
``(notebook_id, run_id)`` only, unparameterized, with ``c.seed = s.seed`` left
as a Join Filter: members x seeds row comparisons (a 41k-object copy's claim
type: 809 M rows filtered, 67 s; a production claim type: tens of hours).
ANALYZE cannot help -- the scratch is empty when it would run.

The pins capture the REAL statements from the store methods (a recording
connection) and EXPLAIN them -- custom and generic (prepared) plans, with the
planner left alone (no ``enable_seqscan=off``) -- in three statistics states:
none, stale (analyzed while the table held another run) and fresh:

* the paged write's INSERT probes ``idx_kg_canonical_scratch_nb_run_seed`` with
  ``seed = s.seed`` in the Index Cond (``CROSS JOIN LATERAL ... OFFSET 0``),
  and drives the scratch side by the page's ordinal range on
  ``idx_kg_cluster_scratch_nb_run_ordinal``; its bound read is a range read of
  that same index;
* Pass B's vector read and the description stage's evidence read never put a
  scratch table on the inner side of a nested loop unparameterized.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_cluster_map_write_plans"),
]

NB = "nb-cmw-plan"
RUN = "run-new"
NOW = "2026-10-21T00:00:00+00:00"
MEMBERS = 3000
SEEDS = 2700
PAGE_ROWS = 100


def _fill(db, run: str) -> None:
    """One run's scratch rows, exactly as Pass A2 and the preparation segment
    write them (claim-like: ~0.9 seeds per member)."""
    db.execute(
        "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) "
        "SELECT %s, %s, 'ko-'||g, 'seed text '||(g %% %s) FROM generate_series(0, %s) g",
        (NB, run, SEEDS, MEMBERS - 1))
    db.execute(
        "INSERT INTO kg_canonical_scratch (notebook_id, run_id, seed, canonical_id, "
        "canonical_name) SELECT %s, %s, 'seed text '||g, 'KL-'||g, 'name '||g "
        "FROM generate_series(0, %s) g",
        (NB, run, SEEDS - 1))


def _seed(postgres_database, statistics: str) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        # deterministic statistics states: no autoanalyze behind the test's back
        db.execute("ALTER TABLE kg_cluster_scratch SET (autovacuum_enabled = false)")
        db.execute("ALTER TABLE kg_canonical_scratch SET (autovacuum_enabled = false)")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'p',%s,%s)",
            (NB, NOW, NOW))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,payload,evidence,"
            "created_at,updated_at) SELECT 'ko-'||g, %s, 'claim', "
            "jsonb_build_object('name','n'||g), "
            "jsonb_build_array(jsonb_build_object('quoted_span','q'||g)), %s, %s "
            "FROM generate_series(0, %s) g", (NB, NOW, NOW, MEMBERS - 1))
        db.execute(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "SELECT 'ko-'||g, %s, '\\x00000000'::bytea, %s "
            "FROM generate_series(0, %s) g", (NB, NOW, MEMBERS - 1))
        db.execute("ANALYZE knowledge_objects")
        db.execute("ANALYZE knowledge_embeddings")
        db.execute("ANALYZE notebooks")
        if statistics == "stale":
            _fill(db, "run-old")
            db.execute("ANALYZE kg_cluster_scratch")
            db.execute("ANALYZE kg_canonical_scratch")
            db.execute("DELETE FROM kg_cluster_scratch")
            db.execute("DELETE FROM kg_canonical_scratch")
        _fill(db, RUN)
        if statistics == "fresh":
            db.execute("ANALYZE kg_cluster_scratch")
            db.execute("ANALYZE kg_canonical_scratch")
    with postgres_database.connect() as db:
        stats = {
            r["tablename"] for r in db.execute(
                "SELECT DISTINCT tablename FROM pg_stats "
                "WHERE schemaname = current_schema() "
                "AND tablename IN ('kg_cluster_scratch', 'kg_canonical_scratch')"
            ).fetchall()
        }
    assert stats == (set() if statistics == "none"
                     else {"kg_cluster_scratch", "kg_canonical_scratch"}), stats


class _Recording:
    """Logs ``(statement, params)`` of every execute. With ``timeouts`` it also
    logs the ``statement_timeout`` in force when each statement starts (read
    on the same connection, so inside the same transaction)."""

    def __init__(self, inner, log, timeouts=None):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_timeouts", timeouts)

    def execute(self, statement, params=(), **kwargs):
        if self._timeouts is not None:
            self._timeouts.append(self._inner.execute(
                "SHOW statement_timeout").fetchone()["statement_timeout"])
        self._log.append((statement, tuple(params or ())))
        return self._inner.execute(statement, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


_PAGE_STATEMENTS = (
    ("floor", lambda s: s.startswith("SELECT set_config('statement_timeout'")),
    ("bound", lambda s: s.startswith("SELECT s.ordinal FROM kg_cluster_scratch")),
    ("insert", lambda s: s.startswith("INSERT INTO concept_clusters")),
)


def _page_roles(log: list) -> list[str]:
    roles = []
    for statement, _params in log:
        text = " ".join(statement.split())
        (role,) = [name for name, matches in _PAGE_STATEMENTS if matches(text)] or ["?"]
        roles.append(role)
    return roles


def _captured(postgres_database) -> dict[str, tuple[str, tuple]]:
    from app.repositories.postgres.unified_kg_store import UnifiedKgStore

    # The first three pages of a production-shaped run (a page is a small
    # slice of the run: 20k of a claim type's millions), each in its own write
    # transaction like the service's. Every page runs exactly: the timeout
    # floor, then the bound read, then the INSERT -- and the bound read and the
    # INSERT both start under the raised floor (the pool's own timeout here is
    # 2 s). Moving the floor below either statement, or out of the page's
    # transaction, is red. Page 2 (both ends real ordinals) is kept for the
    # plans.
    pages: list[list] = []
    start = None
    for _page in range(3):
        log: list = []
        timeouts: list = []
        with postgres_database.write() as db:
            pool_timeout = db.execute("SHOW statement_timeout").fetchone()[
                "statement_timeout"]
            start = UnifiedKgStore.write_cluster_map_generation(
                _Recording(db, log, timeouts), NB, "claim", RUN, NOW, 5,
                start=start, page_rows=PAGE_ROWS)
        assert start is not None
        assert _page_roles(log) == ["floor", "bound", "insert"], [
            s[:50] for s, _p in log]
        assert pool_timeout != "10min"
        assert timeouts == [pool_timeout, "10min", "10min"], timeouts
        pages.append(log)
    log = list(pages[1])
    with postgres_database.connect() as db:
        recording = _Recording(db, log)
        list(UnifiedKgStore.scratch_vector_rows(recording, NB, RUN))
        UnifiedKgStore.cluster_evidence_rows(
            recording, NB, RUN, [f"seed text {g}" for g in range(0, 900, 3)])
    roles = {
        "bound": lambda s: s.startswith("SELECT s.ordinal FROM kg_cluster_scratch"),
        "insert": lambda s: s.startswith("INSERT INTO concept_clusters"),
        "vectors": lambda s: s.startswith("SELECT s.seed AS seed, e.vector"),
        "evidence": lambda s: s.startswith("SELECT s.seed AS seed, k.evidence"),
    }
    found = {}
    for name, matches in roles.items():
        hits = [(s, p) for s, p in log if matches(" ".join(s.split()))]
        assert len(hits) == 1, (name, [h[0][:60] for h in hits])
        found[name] = hits[0]
    return found


def _dollar(statement: str) -> str:
    out, n, i = [], 0, 0
    while i < len(statement):
        if statement.startswith("%%", i):
            out.append("%")
            i += 2
        elif statement.startswith("%s", i):
            n += 1
            out.append(f"${n}")
            i += 2
        else:
            out.append(statement[i])
            i += 1
    return "".join(out)


@contextmanager
def _explaining(postgres_database):
    # EXPLAIN of an INSERT plans it without running it; the write transaction
    # is rolled back anyway so nothing a plan step might touch survives.
    with postgres_database.write() as db:
        try:
            yield db
        finally:
            db.rollback()


def _plan(db, statement: str, params: tuple, *, generic: bool) -> dict:
    from psycopg import sql as pgsql

    if generic:
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        db.execute(f"PREPARE cmw_plan AS {_dollar(statement)}")
        args = ",".join(pgsql.Literal(p).as_string(db) for p in params)
        row = db.execute(
            f"EXPLAIN (COSTS OFF, FORMAT JSON) EXECUTE cmw_plan({args})").fetchone()
        db.execute("DEALLOCATE cmw_plan")
        db.execute("SET LOCAL plan_cache_mode = auto")
    else:
        row = db.execute(
            f"EXPLAIN (COSTS OFF, FORMAT JSON) {statement}", params).fetchone()
    plan = row["QUERY PLAN"]
    plan = json.loads(plan) if isinstance(plan, str) else plan
    return plan[0]["Plan"]


def _nodes(node, parent=None):
    yield node, parent
    for child in node.get("Plans", []):
        yield from _nodes(child, node)


def _scans(plan, relation: str) -> list[dict]:
    return [n for n, _p in _nodes(plan) if n.get("Relation Name") == relation]


def _conditions(node) -> str:
    return " ".join(str(node.get(k, "")) for k in (
        "Index Cond", "Filter", "Recheck Cond", "Hash Cond", "Join Filter",
        "Merge Cond"))


def _assert_no_unparameterized_inner_scratch(name, plan: dict) -> None:
    """Nothing rescans a run's scratch partition once per outer row:
    ``kg_cluster_scratch`` never on the inner side of a nested loop (no index
    of it holds ``object_id`` or ``seed``, so even a "parameterized" inner
    probe of it is a scan of the whole run), ``kg_canonical_scratch`` there
    only as a probe BY the outer row's seed, and no Join Filter on ``seed``
    (the shape of the old members x seeds loop). A Join Filter on a key the
    inner side already probes by (Pass B's ``e.object_id = s.object_id`` next
    to the probe ``e.object_id = k.id``) compares one row per probe and is
    fine."""
    for node, _parent in _nodes(plan):
        assert "seed" not in str(node.get("Join Filter", "")), (
            name, node.get("Join Filter"))
        if node.get("Node Type") != "Nested Loop":
            continue
        inner = [c for c in node.get("Plans", [])
                 if c.get("Parent Relationship") == "Inner"]
        for child in inner:
            for scan, _p in _nodes(child):
                relation = scan.get("Relation Name")
                assert relation != "kg_cluster_scratch", (name, _conditions(scan))
                if relation == "kg_canonical_scratch":
                    cond = str(scan.get("Index Cond", ""))
                    assert re.search(r"seed = [a-z_]+\.seed", cond), (name, cond)


@pytest.mark.parametrize("statistics", ["none", "stale", "fresh"])
def test_scratch_statements_plan_by_key_in_every_statistics_state(
    postgres_database, statistics,
):
    assert PostgresMigrator(postgres_database).migrate() == 83
    _seed(postgres_database, statistics)
    statements = _captured(postgres_database)
    # Statement shape: the page asks for the scratch insertion order itself.
    # Under the index-driven plans below the rows come out in that order
    # anyway, so dropping the ORDER BY would pass every behavioural check on
    # this data; a plan that does not deliver key order (fresh statistics: a
    # hash join under a Sort) would then write in hash order.
    assert " ".join(statements["insert"][0].split()).endswith("ORDER BY s.ordinal")
    # Same for Pass B's vector read, whose order the float32 means depend on.
    assert " ".join(statements["vectors"][0].split()).endswith("ORDER BY s.ordinal")
    with _explaining(postgres_database) as db:
        for generic in (False, True):
            plans = {name: _plan(db, s, p, generic=generic)
                     for name, (s, p) in statements.items()}
            label = (statistics, "generic" if generic else "custom")
            if os.environ.get("CMW_PRINT_PLANS"):
                for name, plan in plans.items():
                    print("=====", label, name)
                    for node, _parent in _nodes(plan):
                        print("   ", node.get("Node Type"), node.get("Relation Name"),
                              node.get("Index Name"), _conditions(node).strip())

            insert = plans["insert"]
            (canonical,) = _scans(insert, "kg_canonical_scratch")
            assert canonical["Node Type"] in ("Index Scan", "Index Only Scan"), (
                label, canonical)
            assert canonical["Index Name"] == "idx_kg_canonical_scratch_nb_run_seed", label
            cond = canonical["Index Cond"]
            for key in ("notebook_id = s.notebook_id", "run_id = s.run_id",
                        "seed = s.seed"):
                assert key in cond, (label, cond)
            # The scratch side reads the page's ordinal range off the keyset
            # index -- a plain or a bitmap index range (fresh statistics on a
            # small test table pick the bitmap) -- never the whole run.
            (scratch,) = _scans(insert, "kg_cluster_scratch")
            if scratch["Node Type"] == "Bitmap Heap Scan":
                (scratch,) = [n for n, _p in _nodes(scratch)
                              if n.get("Node Type") == "Bitmap Index Scan"]
            assert scratch.get("Index Name") == "idx_kg_cluster_scratch_nb_run_ordinal", (
                label, scratch)
            assert "ordinal >=" in scratch["Index Cond"] and \
                "ordinal <=" in scratch["Index Cond"], (label, scratch["Index Cond"])
            # How knowledge_objects is joined is a size-driven choice (on this
            # small table a seq scan + hash is cheaper than per-row probes; on
            # a production table it never is) and carries no rescan risk, so
            # it is not pinned.
            _assert_no_unparameterized_inner_scratch(("insert",) + label, insert)

            (bound,) = _scans(plans["bound"], "kg_cluster_scratch")
            assert bound.get("Index Name") == "idx_kg_cluster_scratch_nb_run_ordinal", (
                label, bound)
            assert "ordinal >=" in bound["Index Cond"], (label, bound)
            assert not [n for n, _p in _nodes(plans["bound"])
                        if n.get("Node Type") == "Sort"], (label, plans["bound"])

            for name in ("vectors", "evidence"):
                _assert_no_unparameterized_inner_scratch((name,) + label, plans[name])

            # Pass B's order is the scratch key: any Sort sorts by s.ordinal,
            # and a plan the scratch index drives through nested loops needs
            # none (before the read ordered by s.ordinal, every one of these
            # plans ended in a Sort on k.ordinal).
            vectors = plans["vectors"]
            sorts = [n for n, _p in _nodes(vectors) if n.get("Node Type") == "Sort"]
            assert all(n["Sort Key"] == ["s.ordinal"] for n in sorts), (label, sorts)
            (driver,) = _scans(vectors, "kg_cluster_scratch")
            hashed = [n for n, _p in _nodes(vectors) if n.get("Node Type") == "Hash Join"]
            if driver.get("Index Name") == "idx_kg_cluster_scratch_nb_run_ordinal" \
                    and not hashed:
                assert sorts == [], (label, sorts)
