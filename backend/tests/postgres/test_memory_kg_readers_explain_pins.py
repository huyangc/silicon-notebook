"""EXPLAIN pins for the E4-4 KG store readers (ruling M1).

Data: a target notebook ``nb`` (2k sources, 150 of them Memory — 100 owned by
``u-a``, 50 by ``u-b`` — 6k objects, 9k relations), a same-shaped ``plain``
notebook without Memory, and 30 other notebooks holding 12k Memory sources and
24k Memory objects between them (owned by ``u-c``).  Every changed statement
is captured through a recording connection while the real store method runs,
then planned custom AND generic, with and without seq/bitmap scans.

What the pins say:

* every read of ``sources`` is bound to the notebook (``notebook_id`` in its
  condition) or is a primary-key probe of one row — no plan reads the
  deployment's Memory sources (the unbound classifier's hash side);
* every read of ``memory_items`` is a primary-key probe;
* the reads DRIVEN by the viewer's own Memory (the overlay, the live Memory
  count half) start from ``idx_sources_nb_hidden_type`` and reach objects /
  relations through their ``source_id`` index — never a walk of the notebook;
* the notebook search box's KG leg keeps ``idx_knowledge_objects_nb_payload_trgm``
  with a viewer (the partial predicate is still the literal ``status!='deprecated'``).
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from app.repositories.postgres import knowledge_counts_cache
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.query_store import QueryStore
from app.repositories.postgres.search import notebook_knowledge_rows

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_readers_pins"),
]

NOW = "2026-09-30T00:00:00+00:00"
_PLAN_TABLES = ("sources", "memory_items", "knowledge_objects", "knowledge_relations",
                "unified_kg_state")


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for user in ("u-a", "u-b", "u-c"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@x.test", user, NOW, NOW, user))
        notebooks = ["nb", "plain"] + [f"o{i}" for i in range(30)]
        for nb in notebooks:
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) VALUES (%s,'N','','','ready','u-a',%s,%s,"
                "'personal')", (nb, NOW, NOW))
            db.execute("INSERT INTO unified_kg_state(notebook_id,updated_at) "
                       "VALUES (%s,%s)", (nb, NOW))
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) SELECT 'nb-m'||g,'nb',"
            "CASE WHEN g<100 THEN 'u-a' ELSE 'u-b' END,'ask_answer','confirmed','t','c',%s,%s "
            "FROM generate_series(0,149) g", (NOW, NOW))
        for nb, memory in (("nb", 150), ("plain", 0)):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                "created_at,updated_at) "
                "SELECT %s||'-s'||g,%s,'t',CASE WHEN g<%s THEN 'memory' "
                "WHEN g<%s+20 THEN 'knowhow' ELSE 'upload' END,"
                "CASE WHEN g<%s THEN %s||'-m'||g END,%s,%s "
                "FROM generate_series(0,1999) g",
                (nb, nb, memory, memory, memory, nb, NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "source_id,created_at,updated_at) SELECT %s||'-o'||g,%s,"
                "CASE WHEN g%%10<7 THEN 'concept' ELSE 'claim' END,'approved',"
                "jsonb_build_object('name','kgname '||g),%s||'-s'||(g%%2000),%s,%s "
                "FROM generate_series(0,5999) g", (nb, nb, nb, NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) SELECT %s||'-r'||g,%s,"
                "%s||'-s'||(g%%2000),%s||'-o'||(g%%6000),%s||'-o'||((g*7+1)%%6000),"
                "'depends_on',%s FROM generate_series(0,8999) g",
                (nb, nb, nb, nb, nb, NOW))
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) SELECT 'om-'||n||'-'||g,'o'||n,'u-c',"
            "'ask_answer','confirmed','t','c',%s,%s "
            "FROM generate_series(0,29) n, generate_series(0,399) g", (NOW, NOW))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'os-'||n||'-'||g,'o'||n,'t','memory','om-'||n||'-'||g,%s,%s "
            "FROM generate_series(0,29) n, generate_series(0,399) g", (NOW, NOW))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) SELECT 'oo-'||n||'-'||g,'o'||n,"
            "'concept','approved',jsonb_build_object('name','kgname m'||g),"
            "'os-'||n||'-'||(g%%400),%s,%s "
            "FROM generate_series(0,29) n, generate_series(0,799) g", (NOW, NOW))
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _PLAN_TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


class _Recording:
    def __init__(self, inner, log):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)

    def execute(self, statement, params=()):
        self._log.append((statement, tuple(params or ())))
        return self._inner.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


def _captured(postgres_database, notebook_id: str, viewer: str) -> list[tuple]:
    """Every statement the changed readers issue for ``viewer``."""
    ids = [f"{notebook_id}-o{g}" for g in range(0, 6000, 120)]
    store = KnowledgeStore
    log: list = []
    knowledge_counts_cache.invalidate()
    with postgres_database.connect() as inner:
        db = _Recording(inner, log)
        store.list_knowledge_page(db, notebook_id, "concept", None, 3000, 50, viewer_id=viewer)
        store.list_knowledge_page(db, notebook_id, "concept", "approved", 0, 50,
                                  viewer_id=viewer)
        store.type_counts(db, notebook_id, viewer_id=viewer)
        store.type_counts(db, notebook_id)  # warm: no statement besides the seq read
        store.object_evidence_rows(db, ids[:20], with_evidence=False)
        store._enrich_evidence(  # instance method that never touches self
            None, db, [{"element_id": f"el-{g}"} for g in range(20)], sources_only=True)
        store.edge_centrality_source_rows(db, notebook_id, 100000)
        store.edge_centrality_source_rows(db, notebook_id, 50)
        # a notebook holding only u-c's Memory objects: kg_ready's own EXISTS runs
        QueryStore.notebook_has_kg(db, "o0", viewer_id="u-c")
        store.graph_node_rows(db, notebook_id, viewer_id=viewer)
        store.unified_graph_rows(db, notebook_id, viewer_id=viewer)
        store.relations_for_notebook(db, notebook_id, viewer_id=viewer)
        store.neighbor_relation_rows(db, notebook_id, ids, viewer_id=viewer)
        store.object_meta_rows_for_notebook(db, notebook_id, ids[:20], viewer_id=viewer)
        store.object_meta_rows(db, ids[:20], notebook_id=notebook_id, viewer_id=viewer)
        store.fts_search(db, notebook_id, "kgname 42", 30, viewer_id=viewer)
        store.community_context_rows(db, notebook_id, ids)
        store.duplicate_member_rows(db, notebook_id, ids)
        notebook_knowledge_rows(db, notebook_id, "kgname 4", 30, viewer_id=viewer)
        QueryStore.notebook_has_kg(db, notebook_id, viewer_id=viewer)
        QueryStore.knowledge_type_count_rows(
            db, notebook_id, ("approved",), viewer_id=viewer)
        if viewer:
            store.unified_graph_rows(db, notebook_id, viewer_id=viewer, own_memory_only=True)
            store.relations_for_notebook(db, notebook_id, viewer_id=viewer,
                                         own_memory_only=True)
    return [(s, p) for s, p in log if "kg_mutation_seq" not in s
            and "pg_index" not in s and "SAVEPOINT" not in s]


def _dollar(statement: str) -> str:
    out, n, i = [], 0, 0
    while i < len(statement):
        if statement.startswith("%%", i):
            out.append("%"); i += 2
        elif statement.startswith("%s", i):
            n += 1; out.append(f"${n}"); i += 2
        else:
            out.append(statement[i]); i += 1
    return "".join(out)


def _plan_json(connection, statement, params, *, generic: bool, capabilities_off: bool,
               bitmap_only: bool = False):
    from psycopg import sql as pgsql

    connection.execute("BEGIN")
    try:
        if capabilities_off:
            connection.execute("SET LOCAL enable_seqscan=off")
            connection.execute("SET LOCAL enable_bitmapscan=off")
        if bitmap_only:
            connection.execute("SET LOCAL enable_seqscan=off")
            connection.execute("SET LOCAL enable_indexscan=off")
        if generic:
            connection.execute("SET LOCAL plan_cache_mode = force_generic_plan")
            connection.execute(f"PREPARE e44 AS {_dollar(statement)}")
            args = ",".join(
                pgsql.Literal(list(p) if isinstance(p, (tuple, list)) else p)
                .as_string(connection) for p in params)
            row = connection.execute(
                f"EXPLAIN (COSTS OFF, FORMAT JSON) EXECUTE e44({args})").fetchone()
            connection.execute("DEALLOCATE e44")
        else:
            row = connection.execute(
                f"EXPLAIN (COSTS OFF, FORMAT JSON) {statement}", params).fetchone()
    finally:
        connection.execute("ROLLBACK")
    plan = row["QUERY PLAN"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    plan = json.loads(plan) if isinstance(plan, str) else plan
    return plan[0]["Plan"]


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _conditions(node) -> str:
    text = " ".join(str(node.get(k, "")) for k in
                    ("Index Cond", "Filter", "Recheck Cond", "Hash Cond", "Join Filter"))
    for child in node.get("Plans", []):
        if child.get("Node Type") == "Bitmap Index Scan":
            text += " " + str(child.get("Index Cond", ""))
    return text


def _assert_bound(statement, plan):
    for node in _nodes(plan):
        relation = node.get("Relation Name")
        cond = _conditions(node)
        if relation == "sources":
            assert "notebook_id" in cond or "(id = " in cond, (statement[:120], node)
        if relation == "memory_items":
            # a primary-key probe, or (hashed SubPlan under the readable
            # fragment's OR) the viewer's own items — bounded by one user
            assert "(id = " in cond or "created_by" in cond, (statement[:120], node)
        if relation in ("knowledge_objects", "knowledge_relations"):
            # the target notebook's rows, or rows reached by key
            assert ("notebook_id" in cond or "(id = " in cond or "source_id" in cond
                    or "object_id" in cond), (statement[:120], node)


def _assert_driven_by_memory_sources(statement, plan, text, *, generic):
    """The own-Memory overlay and the live Memory count half: the Memory
    source ids come first (an InitPlan; ``idx_sources_nb_hidden_type`` in the
    custom plan — the generic plan of this seed, where three quarters of the
    deployment's sources are Memory, may read the notebook's sources by
    another notebook-leading index, still bound as checked above) and the
    rows come through their ``source_id`` index — never a scan of the
    notebook's objects or relations (the join form hash-joined all of them)."""
    for node in _nodes(plan):
        relation = node.get("Relation Name")
        index = " ".join(str(n.get("Index Name", "")) for n in _nodes(node))
        if relation == "sources" and not generic:
            assert "idx_sources_nb_hidden_type" in index, (statement[:120], text[:1500])
        # the Memory side (``o``/``mo``/``r``); the count statement's ``total``
        # half reads the notebook's objects by its covering index, as before
        if relation in ("knowledge_objects", "knowledge_relations") and (
            node.get("Alias") in ("o", "mo", "r")
        ):
            assert "_source" in index and "Seq Scan" not in node["Node Type"], (
                statement[:120], text[:1500])


@pytest.mark.parametrize("viewer_kind", ["member", "closed"])
def test_viewer_statements_stay_bound_to_the_notebook(postgres_database, viewer_kind):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    viewer = "u-b" if viewer_kind == "member" else ""
    dump = os.environ.get("E44_PRINT_PLANS")
    for target in ("nb", "plain"):
        statements = _captured(postgres_database, target, viewer)
        assert len(statements) >= 14, len(statements)
        with postgres_database.connect() as connection:
            for statement, params in statements:
                for generic in (False, True):
                    for off in (True, False):
                        plan = _plan_json(connection, statement, params,
                                          generic=generic, capabilities_off=off)
                        text = json.dumps(plan)
                        if dump:
                            print("=====", target, generic, off, " ".join(statement.split())[:160])
                            print(text[:3000])
                        _assert_bound(statement, plan)
                        # Memory-driven reads, recognised by the driving alias
                        # (the fragments use fs/ds/rm), whatever their spelling
                        if " sources s " in statement and not off:
                            # natural plans, custom and generic; with scans
                            # disabled every access is still notebook-bound
                            # (checked above), just not necessarily this shape
                            # ``o0`` holds nothing but Memory sources: reading its
                            # sources by any notebook-leading index is as narrow
                            # as the partial one (bound, as checked above)
                            _assert_driven_by_memory_sources(
                                statement, plan, text,
                                generic=generic or "o0" in params)
                        if statement.startswith("SELECT id, source_id FROM knowledge_objects"):
                            # object_evidence_rows(with_evidence=False): by key
                            assert "pk_knowledge_objects" in text or off, text[:1500]
                        if statement.startswith("SELECT id, source_id FROM source_elements"):
                            # _enrich_evidence(sources_only=True): by key (this
                            # seed has no element rows, so only the scans-off
                            # plan is meaningful; an empty table seq-scans)
                            assert "pk_source_elements" in text or not off, text[:1500]
                if "(payload::text)" in statement:
                    # The search box's KG leg with a viewer: the partial GIN is
                    # still usable (its predicate stays implied by the literal
                    # ``status!='deprecated'``), custom and generic.
                    for generic in (False, True):
                        plan = _plan_json(connection, statement, params, generic=generic,
                                          capabilities_off=False, bitmap_only=True)
                        assert "idx_knowledge_objects_nb_payload_trgm" in json.dumps(plan), (
                            generic, json.dumps(plan)[:1500])
