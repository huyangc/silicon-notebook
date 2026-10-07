"""E4-2 on PostgreSQL (the primary environment): graph-build inputs and the
derived-layer readers leave Memory-derived objects out, the rebuild end-write
clears dirty only when nothing changed mid-rebuild, and EXPLAIN pins for every
changed statement.

Twin of the SQLite tests in ``test_rebuild_streaming.py`` /
``test_rebuild_communities.py`` / ``test_memory_derived_in_notebook.py`` (same
world: ``tests/memory_kg_seed_world.py``).

The pins capture the statements from the real store methods (a recording
wrapper around the connection) and EXPLAIN them with custom AND generic plans,
over a data set where OTHER notebooks hold many Memory sources: the Memory side
of every anti join must be bound to the target notebook (never a scan of the
deployment's Memory sources or objects), and none may become a correlated
per-row SubPlan. Judged twice, as the sibling pins are: with seqscan/bitmapscan
off (an index path exists) and under the real planner on ANALYZEd data.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.repositories.postgres import memory_sql as pg_memory_sql
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.repository import PostgresRepository
from app.services.embedding import FakeEmbedder
from tests import memory_kg_seed_world as world
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_seed_exclusion"),
]

NOW = "2026-09-30T00:00:00+00:00"


@pytest.fixture
def repo(postgres_settings: Settings):
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def _state(repo, notebook_id):
    return world.rows(repo, "SELECT dirty, kg_mutation_seq, cluster_generation "
                            "FROM unified_kg_state WHERE notebook_id=?", (notebook_id,))[0]


# ------------------------------------------------------------- behaviour

def test_a_rebuild_puts_no_memory_derived_object_in_any_cluster_pg(repo):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    layers = world.assert_no_memory_in_derived_layers(repo, nb_id)
    assert len([r for r in layers["clusters"]
                if r["canonical_id"] == "K-grouped query attention"]) == 2
    assert layers["canonical_relations"] and layers["mention_edges"]
    assert layers["community_members"]
    assert _state(repo, nb_id)["dirty"] == 0


def test_seed_streams_and_mention_claims_leave_memory_out_pg(repo):
    nb_id = world.seed(repo)
    store = repo._runtime.unified_kg
    memory = world.memory_object_ids(repo, nb_id)
    with repo._connect() as db:
        names = [json.loads(r["payload"])["name"]
                 for r in store.seed_payload_rows(db, nb_id, "concept")]
        streamed = [r["id"] for r in store.stream_seed_rows(db, nb_id, "concept")]
        _clusters, claims = store.mention_seed_rows(db, nb_id)
    assert names == ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)",
                     "Grouped-query attention (GQA)", "KV cache"]
    assert len(streamed) == 4 and not set(streamed) & memory
    assert [r["nm"] for r in claims] == [
        "GQA uses fewer KV heads than MQA while keeping quality."]


def test_a_memory_source_deleted_mid_rebuild_leaves_the_notebook_dirty_pg(
    repo, monkeypatch,
):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    assert _state(repo, nb_id)["dirty"] == 0
    store = repo._runtime.unified_kg
    real = store.stream_seed_rows
    fired = []

    def stream_seed_rows(db, notebook_id, object_type):
        if not fired:
            fired.append(object_type)
            repo.delete_source(world.MEMORY_SOURCE)
        return real(db, notebook_id, object_type)

    monkeypatch.setattr(store, "stream_seed_rows", stream_seed_rows)
    before = _state(repo, nb_id)["kg_mutation_seq"]
    repo.rebuild_unified_kg(nb_id, force=True)
    after = _state(repo, nb_id)
    assert fired and after["kg_mutation_seq"] > before
    assert after["dirty"] == 1, after
    monkeypatch.setattr(store, "stream_seed_rows", real)
    repo.rebuild_unified_kg(nb_id)
    assert _state(repo, nb_id)["dirty"] == 0


def test_finish_rebuild_state_clears_dirty_only_at_the_claimed_seq_pg(repo):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    state = _state(repo, nb_id)
    gen, seq = int(state["cluster_generation"]), int(state["kg_mutation_seq"])
    store = repo._runtime.unified_kg

    def finish(**kw):
        with repo._write() as db:
            db.execute("UPDATE unified_kg_state SET dirty=1 WHERE notebook_id=%s", (nb_id,))
            store.finish_rebuild_state(db, nb_id, "v", 1, NOW,
                                       published_generation=kw.pop("gen", gen), **kw)
        return _state(repo, nb_id)["dirty"]

    assert finish(input_seq=seq) == 0
    assert finish(input_seq=seq - 1) == 1
    assert finish() == 1
    assert finish(input_seq=seq, gen=gen + 5) == 1


def test_finish_rebuild_state_insert_branch_follows_the_same_rule_pg(repo):
    nb_id = repo.create_notebook(world.NotebookCreate(name="nb")).id
    store = repo._runtime.unified_kg

    def finish_on_a_missing_row(input_seq):
        with repo._write() as db:
            db.execute("DELETE FROM unified_kg_state WHERE notebook_id=%s", (nb_id,))
            store.finish_rebuild_state(db, nb_id, "v", 1, NOW,
                                       published_generation=0, input_seq=input_seq)
        state = world.rows(repo, "SELECT dirty, kg_mutation_seq, cluster_input_version "
                                 "FROM unified_kg_state WHERE notebook_id=?", (nb_id,))[0]
        assert state["kg_mutation_seq"] == 0 and state["cluster_input_version"] == "v"
        return state["dirty"]

    assert finish_on_a_missing_row(0) == 0
    assert finish_on_a_missing_row(3) == 1
    assert finish_on_a_missing_row(None) == 1


def test_a_manual_rebuild_sets_the_memory_isolation_marker_pg(repo):
    present = world.rows(
        repo,
        "SELECT count(*) AS n FROM information_schema.columns WHERE "
        "table_schema = current_schema() AND table_name = 'unified_kg_state' "
        "AND column_name = 'memory_isolation_version'", ())[0]["n"]
    if not present:
        pytest.skip("unified_kg_state.memory_isolation_version arrives with "
                    "PostgreSQL 0067 (E4-5); flips on at PR-E4 assembly")
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    with repo._write() as db:
        db.execute("UPDATE unified_kg_state SET memory_isolation_version=0 "
                   "WHERE notebook_id=%s", (nb_id,))
    repo.rebuild_unified_kg(nb_id, force=True)
    assert world.rows(repo, "SELECT memory_isolation_version AS v FROM unified_kg_state "
                            "WHERE notebook_id=?", (nb_id,))[0]["v"] == 1


def test_summary_rows_drop_memory_members_of_a_pre_isolation_community_pg(repo):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    memory = world.object_ids_by_name(repo, nb_id, world.MEMORY_SOURCE)
    legacy = ["K-kv cache", memory["SECRET-ALPHA plan"], "K-grouped query attention",
              memory["Secret Beta budget"]]
    gen = world.rows(repo, "SELECT community_generation AS g FROM unified_kg_state "
                           "WHERE notebook_id=?", (nb_id,))[0]["g"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at, "
            "generation) VALUES ('comm-legacy', %s, 0, %s::jsonb, 4, %s, %s)",
            (nb_id, json.dumps(legacy), NOW, gen),
        )
    store = repo._runtime.unified_kg
    with repo._connect() as db:
        rows = {r["id"]: json.loads(r["member_ids"])
                for r in store.community_rows_for_summary(db, nb_id, 0)}
    assert rows["comm-legacy"] == ["K-kv cache", "K-grouped query attention"]
    assert all(not set(ids) & set(memory.values()) for ids in rows.values())


def test_memory_derived_in_notebook_classifies_this_notebooks_rows_only_pg(repo):
    nb_id = world.seed(repo)
    other = repo.create_notebook(world.NotebookCreate(name="other")).id
    with repo._connect() as db:
        here = {r["id"] for r in db.execute(
            "SELECT o.id FROM knowledge_objects o WHERE o.notebook_id=%s AND "
            + pg_memory_sql.memory_derived_in_notebook("o"), (nb_id,)).fetchall()}
        cross = db.execute(
            "SELECT " + pg_memory_sql.memory_derived_in_notebook("x") + " AS here, "
            + pg_memory_sql.memory_derived_object("x") + " AS anywhere "
            "FROM (SELECT %s::text AS source_id, %s::text AS notebook_id) x",
            (world.MEMORY_SOURCE, other),
        ).fetchone()
    assert here == world.memory_object_ids(repo, nb_id)
    assert (cross["here"], cross["anywhere"]) == (False, True)


# ------------------------------------------------------------ EXPLAIN pins

_PLAN_TABLES = ("sources", "memory_items", "knowledge_objects", "knowledge_relations",
                "concept_clusters", "communities", "unified_kg_state")


def _seed_plan_data(postgres_database) -> None:
    """Target ``nb`` (6k objects, 9k relations, 4k cluster rows, 150 Memory
    sources with 2 objects each, 1 community), a target ``plain`` of the same
    shape WITHOUT Memory, and 30 other notebooks holding 400 Memory sources
    and 800 Memory objects each (12k / 24k deployment-wide)."""
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@x.test','a','user','active',%s,%s,'a','','',0)", (NOW, NOW))
        notebooks = ["nb", "plain"] + [f"o{i}" for i in range(30)]
        for nb in notebooks:
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) VALUES (%s,'N','','','ready','u-a',%s,%s,"
                "'personal')", (nb, NOW, NOW))
            db.execute("INSERT INTO unified_kg_state(notebook_id,updated_at) "
                       "VALUES (%s,%s)", (nb, NOW))
        for nb, memory in (("nb", 150), ("plain", 0)):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                "SELECT %s||'-s'||g,%s,'t',CASE WHEN g<%s THEN 'memory' "
                "WHEN g<%s+20 THEN 'knowhow' ELSE 'upload' END,%s,%s "
                "FROM generate_series(0,1999) g", (nb, nb, memory, memory, NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "source_id,created_at,updated_at) SELECT %s||'-o'||g,%s,"
                "CASE WHEN g%%10<7 THEN 'concept' ELSE 'claim' END,'approved',"
                "jsonb_build_object('name','n'||g),%s||'-s'||(g%%2000),%s,%s "
                "FROM generate_series(0,5999) g", (nb, nb, nb, NOW, NOW))
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) SELECT %s||'-r'||g,%s,"
                "%s||'-s'||(g%%2000),%s||'-o'||(g%%6000),%s||'-o'||((g*7+1)%%6000),"
                "'depends_on',%s FROM generate_series(0,8999) g",
                (nb, nb, nb, nb, nb, NOW))
            db.execute(
                "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,created_at,generation) SELECT %s||'-c'||g,%s,"
                "'K-c'||(g/3),%s||'-o'||g,'n','concept',%s,0 "
                "FROM generate_series(0,5999) g WHERE g%%10<7", (nb, nb, nb, NOW))
            db.execute(
                "INSERT INTO communities(id,notebook_id,level,member_ids,size,created_at,"
                "generation) VALUES (%s,%s,0,%s::jsonb,3,%s,0)",
                (f"{nb}-comm", nb, json.dumps([f"{nb}-o0", "K-c1", f"{nb}-o9"]), NOW))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'os-'||n||'-'||g,'o'||n,'t','memory',%s,%s "
            "FROM generate_series(0,29) n, generate_series(0,399) g", (NOW, NOW))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) SELECT 'oo-'||n||'-'||g,'o'||n,'concept',"
            "'approved',jsonb_build_object('name','m'||g),'os-'||n||'-'||(g%%400),%s,%s "
            "FROM generate_series(0,29) n, generate_series(0,799) g", (NOW, NOW))
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _PLAN_TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


class _Recording:
    """Connection wrapper that logs ``(statement, params)`` of every execute;
    attribute writes (``read_only`` / ``isolation_level``) go to the real one."""

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


def _captured(postgres_database, notebook_id: str) -> dict[str, tuple]:
    from app.repositories.postgres.unified_kg_store import UnifiedKgStore

    log: list = []

    class _Db:
        def __getattr__(self, name):
            return getattr(postgres_database, name)

        @contextmanager
        def connect(self):
            with postgres_database.connect() as db:
                yield _Recording(db, log)

    store = UnifiedKgStore(_Db())
    with store.database.connect() as db:
        list(store.seed_payload_rows(db, notebook_id, "concept"))
        list(store.stream_seed_rows(db, notebook_id, "concept"))
        list(store.canonical_relation_seed_rows(db, notebook_id))
        store.mention_seed_rows(db, notebook_id)
        list(store.community_graph_rows(db, notebook_id)[1])
        store.community_rows_for_summary(db, notebook_id, 0)
        store.catchup_window_members(db, notebook_id, 0, "2026-01-01T00:00:00+00:00", 5, 100)
    store.cluster_size_histogram(notebook_id)
    store.largest_clusters(notebook_id)
    store.relation_provenance_counts(notebook_id)
    roles = {
        "seed_payload": lambda s: s.startswith("SELECT o.payload::text"),
        "stream_seed": lambda s: s.startswith("SELECT o.id,o.payload::text"),
        "canonical_relations": lambda s: "AS src_doc" in s,
        "mention_clusters": lambda s: "AS cname" in s,
        "mention_claims": lambda s: s.startswith("SELECT o.id,") and "AS nm" in s,
        "community_graph": lambda s: "FROM knowledge_relations kr" in s and "ORDER BY kr.id" in s,
        "community_summary": lambda s: "FROM communities" in s and "member_ids" in s,
        "histogram": lambda s: "n_excluded" in s,
        "largest": lambda s: "AS members" in s,
        "provenance": lambda s: "unknown_bucket" in s,
        "probe": lambda s: s.startswith("SELECT EXISTS(SELECT 1 FROM sources"),
        "catchup": lambda s: "make_interval" in s,
    }
    found = {}
    for name, matches in roles.items():
        hits = [(s, p) for s, p in log if matches(" ".join(s.split()))]
        assert len(hits) == 1, (name, [h[0][:80] for h in hits])
        found[name] = hits[0]
    return found


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


def _plan_json(connection, statement, params, *, generic: bool, capabilities_off: bool):
    from psycopg import sql as pgsql

    if capabilities_off:
        connection.execute("SET LOCAL enable_seqscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
    if generic:
        connection.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        connection.execute(f"PREPARE e42 AS {_dollar(statement)}")
        args = ",".join(
            pgsql.Literal(list(p) if isinstance(p, tuple) else p).as_string(connection)
            for p in params)
        row = connection.execute(f"EXPLAIN (COSTS OFF, FORMAT JSON) EXECUTE e42({args})"
                                 ).fetchone()
        connection.execute("DEALLOCATE e42")
    else:
        row = connection.execute(f"EXPLAIN (COSTS OFF, FORMAT JSON) {statement}",
                                 params).fetchone()
    plan = row["QUERY PLAN"]
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


_MEMORY_SIDE = {"ds", "ds_1", "ds_2", "ds_3", "xs", "xt", "xo", "xm"}


def _assert_notebook_bound(name, plan):
    """Every read of sources / knowledge_objects on the Memory side of an anti
    join is bound to the notebook (or is a primary-key probe of one row): no
    plan reads the deployment's Memory sources or objects."""
    for node in _nodes(plan):
        relation, alias = node.get("Relation Name"), node.get("Alias")
        if relation not in ("sources", "knowledge_objects") or alias not in _MEMORY_SIDE:
            continue
        cond = _conditions(node)
        assert "notebook_id" in cond or "(id = " in cond, (name, alias, node["Node Type"], cond)


_FRAGMENTS = {
    "seed_payload": 1, "stream_seed": 1, "canonical_relations": 3,
    "mention_clusters": 1, "mention_claims": 1, "community_graph": 3,
    "community_summary": 1, "histogram": 1, "largest": 1, "provenance": 1,
    "probe": 0, "catchup": 1,
}


def test_memory_exclusion_statements_are_bound_to_the_notebook(postgres_database):
    assert PostgresMigrator(postgres_database).migrate()
    _seed_plan_data(postgres_database)
    dump = os.environ.get("E42_PRINT_PLANS")
    for target in ("nb", "plain"):
        statements = _captured(postgres_database, target)
        for name, (statement, _params) in statements.items():
            # every reader's statement carries each of its notebook-bound Memory
            # fragments (the relation plus both endpoints for the two relation
            # graphs): deleting one is red here, not silently unasserted
            expected = _FRAGMENTS[name]
            if target == "plain" and name == "community_summary":
                expected = 0
            assert statement.count("ds.notebook_id") == expected, (target, name)
        if target == "plain":
            # no Memory source: the summary read is the pre-E4-2 statement
            assert "jsonb_agg" not in statements["community_summary"][0]
        else:
            assert "jsonb_agg" in statements["community_summary"][0]
        with postgres_database.connect() as connection:
            for name, (statement, params) in statements.items():
                for generic in (False, True):
                    for off in (True, False):
                        plan = _plan_json(connection, statement, params,
                                          generic=generic, capabilities_off=off)
                        text = json.dumps(plan)
                        if dump:
                            print("=====", target, name, generic, off, text[:4000])
                        _assert_notebook_bound(name, plan)
                        # never a scan of the whole sources table
                        for node in _nodes(plan):
                            if node.get("Relation Name") == "sources":
                                assert "notebook_id" in _conditions(node) or \
                                    "(id = " in _conditions(node), (name, node)
                        if name == "community_summary" and target == "nb":
                            # one bounded member expansion per community
                            continue
                        if name == "catchup":
                            # its in-flight-generation exclusion was a hashed
                            # SubPlan over the one state row before E4-2; the
                            # Memory probe must not add a SubPlan of its own
                            assert not [
                                n for n in _nodes(plan)
                                if n.get("Parent Relationship") == "SubPlan"
                                and n.get("Relation Name") != "unified_kg_state"
                            ], (target, name, generic, off)
                            continue
                        assert "SubPlan" not in text, (target, name, generic, off)
                        if off and "ds.notebook_id" in statement:
                            assert "idx_sources_nb_hidden_type" in text or \
                                "pk_sources" in text, (target, name, generic)


def test_catchup_window_never_republishes_a_memory_member_pg(repo):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    memory = world.object_ids_by_name(repo, nb_id, world.MEMORY_SOURCE)
    shared = world.object_ids_by_name(repo, nb_id, "s2")
    gen = int(_state(repo, nb_id)["cluster_generation"])
    with repo._write() as db:
        for row_id, member in (("cc-win-mem", memory["SECRET-ALPHA plan"]),
                               ("cc-win-shared", shared["KV cache"])):
            db.execute(
                "INSERT INTO concept_clusters (id, notebook_id, canonical_id, "
                "member_object_id, canonical_name, object_type, created_at, generation) "
                "VALUES (%s, %s, 'K-window', %s, 'window', 'concept', %s, %s)",
                (row_id, nb_id, member, NOW, gen + 7),
            )
    with repo._connect() as db:
        rows = repo._runtime.unified_kg.catchup_window_members(
            db, nb_id, gen, "2026-01-01T00:00:00+00:00", 5, 100)
    members = {r["member_object_id"] for r in rows}
    assert shared["KV cache"] in members
    assert not members & world.memory_object_ids(repo, nb_id)


# ------------------------------------------------ legacy shapes (twins)
# Bodies shared with the SQLite tests (tests/memory_kg_seed_world.py): removing
# any one PostgreSQL reader's exclusion turns one of these red.

def test_canonical_relations_and_communities_leave_legacy_memory_endpoints_out_pg(repo):
    world.assert_legacy_memory_endpoints_stay_out(repo)


def test_memory_claims_and_memory_members_build_no_mention_bridge_pg(repo):
    world.assert_memory_builds_no_mention_bridge(repo)


def test_analysis_readers_do_not_count_rank_or_name_memory_derived_rows_pg(repo):
    world.assert_analysis_readers_skip_legacy_memory_rows(repo)
