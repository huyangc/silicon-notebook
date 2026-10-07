"""EXPLAIN pins for the Memory exclusion inside ``IndexProjectionStore`` (E4-6).

The store keeps Memory-derived rows out of what a shared artifact reads: the
paged object / relation reads carry NO Memory predicate (they add ``source_id``
and are matched in Python against the notebook's Memory source set, read once),
the cluster canonicals and the Memory object / relation id sets are read once,
driven by THIS notebook's Memory source ids, plus the probe and source reads.
New SQL gets a plan pin so a later edit cannot silently put a per-page anti join
back on a multi-million-row notebook, or let a Memory-set read scan every
notebook's Memory (the seed holds a second notebook with ten times the Memory).

The statements are CAPTURED from the real store methods (a recording wrapper
around the connection they use) and then EXPLAINed, so the pin is on the text
that actually runs, never on a copy of it. No per-row ``SubPlan`` in any plan:
the only SubPlan allowed is the *hashed* one of the row reads' Memory exclusion
(evaluated once per statement).

Judged with seqscan/bitmapscan off, as the sibling pins are: the test bed has
thousands of rows and no production-scale cost separation, but a statement
rewritten into a shape with no index path shows up as a Seq Scan.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from itertools import islice

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_index_projection_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_TABLES = (
    "sources",
    "knowledge_objects",
    "knowledge_relations",
    "concept_clusters",
    "chunks",
)


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@example.test','a','user','active',%s,%s,'a','','',0)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'src-'||g,'nb','t',CASE WHEN g<200 THEN 'memory' "
            "WHEN g<400 THEN 'knowhow' ELSE 'upload' END,%s,%s "
            "FROM generate_series(0,3999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) SELECT 'ko-'||g,'nb','concept','approved',"
            "jsonb_build_object('name','n'||g),'src-'||(g%%4000),%s,%s "
            "FROM generate_series(0,29999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) SELECT 'kr-'||g,'nb','src-'||(g%%4000),"
            "'ko-'||g,'ko-'||(g+1),'depends_on',%s FROM generate_series(0,29999) g",
            (_NOW,),
        )
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-'||g,'nb','can-'||(g/3),'ko-'||g,'n','concept',%s,0 "
            "FROM generate_series(0,17999) g",
            (_NOW,),
        )
        # Another notebook's Memory, ten times this one's: a Memory-set read
        # that is not bounded by the notebook shows up in its plan.
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb-other','O','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'other-src-'||g,'nb-other','t','memory',%s,%s "
            "FROM generate_series(0,1999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) SELECT 'other-ko-'||g,'nb-other','concept',"
            "'approved',jsonb_build_object('name','o'||g),'other-src-'||(g%%2000),%s,%s "
            "FROM generate_series(0,19999) g",
            (_NOW, _NOW),
        )
        for table in _TABLES:
            db.execute(f"ANALYZE {table}")
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _no_per_row_subplan(plan: str) -> bool:
    """Every ``SubPlan`` a plan condition refers to is a *hashed* one (run once
    per statement); the ``SubPlan N`` node headers themselves are not
    references."""
    import re

    for line in plan.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"SubPlan \d+( \(returns \$\d+\))?", stripped):
            continue
        for match in re.finditer(r"(hashed )?SubPlan \d+", stripped):
            if not match.group(1):
                return False
    return True


def _sources_bounded_by_notebook(plan: str) -> bool:
    """Every scan of ``sources`` in ``plan`` has ``notebook_id`` in one of ITS
    OWN condition lines (the ``Index Cond`` / ``Filter`` / ``Recheck Cond``
    lines directly under the scan node, indented deeper than it -- never a
    neighbouring node's), and there is at least one such scan."""
    lines = plan.splitlines()

    def own_conditions(at: int) -> list:
        depth = len(lines[at]) - len(lines[at].lstrip())
        found = []
        for line in lines[at + 1:]:
            stripped = line.lstrip()
            if len(line) - len(stripped) <= depth or stripped.startswith("->"):
                break
            if stripped.startswith(("Index Cond:", "Filter:", "Recheck Cond:")):
                found.append(stripped)
        return found

    scans = [i for i, line in enumerate(lines) if " on sources" in line]
    return bool(scans) and all(
        any("notebook_id" in cond for cond in own_conditions(i)) for i in scans
    )


class _Recording:
    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, params=()):
        self._log.append((statement, tuple(params or ())))
        return self._inner.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _plan(connection, statement: str, params: tuple, capabilities_off: bool = True) -> str:
    if capabilities_off:
        connection.execute("SET LOCAL enable_seqscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {statement}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _captured_plans(postgres_database, capabilities_off: bool = True) -> dict[str, list[str]]:
    from app.core.config import Settings
    from app.repositories.postgres.index_projection_store import IndexProjectionStore

    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    settings = Settings(
        database_url=postgres_database.settings.database_url,
        graph_fetch_page_rows=1000,
    )
    log: list = []

    @contextmanager
    def recording_connect():
        with postgres_database.connect() as db:
            yield _Recording(db, log)

    store = IndexProjectionStore(
        settings,
        connect=recording_connect,
        in_batches=lambda ids: [list(ids)],
        ent_chunk_map=lambda _nb, paged=False: {},
        mention_extra_edges=lambda _nb: [],
        vector_matrix=lambda *_a, **_k: ([], None),
    )
    with recording_connect() as db:
        # past the first 1000-row page, so a second statement carries `ordinal>`
        list(islice(store.active_object_graph_rows(db, "nb"), 1001))
        store.active_relation_graph_rows(db, "nb")
    store.graph_rows("nb", None, synonym_edges=[])
    store.memory_cluster_canonicals("nb")
    store.memory_derived_ids("nb", "knowledge_embeddings")
    store.memory_derived_ids("nb", "relation_embeddings")
    store.version_facts("nb")
    store.shared_content_digest("nb")
    store.memory_source_ids("nb")
    store.source_ids("nb")
    with recording_connect() as db:
        store.relation_ids_for_source_batch(db, "nb", ["src-1", "src-2"])

    roles = {
        "objects_page": lambda s: "AS name,ordinal" in s and "ordinal>" not in s,
        "objects_next_page": lambda s: "AS name,ordinal" in s and "ordinal>" in s,
        "relations_rows": lambda s: "SELECT source_object_id, target_object_id, edge_type"
        in s and "review_status" not in s and "FROM knowledge_relations" in s,
        "graph_objects": lambda s: "SELECT id, object_type, payload, ordinal" in s,
        "graph_relations": lambda s: "review_status!='rejected'" in s,
        "graph_clusters": lambda s: "SELECT canonical_id, member_object_id" in s,
        "cluster_canonicals": lambda s: s.startswith("SELECT DISTINCT c.canonical_id"),
        "object_ids": lambda s: s.startswith("SELECT id FROM knowledge_objects"),
        "relation_ids": lambda s: s.startswith("SELECT id FROM knowledge_relations")
        and "source_id IN" not in s,
        "delta_relation_ids": lambda s: s.startswith("SELECT id FROM knowledge_relations")
        and "source_id IN" in s,
        "has_memory": lambda s: s.startswith("SELECT EXISTS(SELECT 1 FROM sources"),
        "memory_source_ids": lambda s: s.startswith("SELECT id FROM sources")
        and "NOT (" not in s,
        "source_ids": lambda s: s.startswith("SELECT id FROM sources") and "NOT (" in s,
        # shared_content_digest: version_facts' aggregates without Memory rows
        "shared_objects": lambda s: s.startswith("SELECT COUNT(*)")
        and "FROM knowledge_objects" in s and "NOT IN" in s,
        "shared_relations": lambda s: s.startswith("SELECT COUNT(*)")
        and "FROM knowledge_relations" in s and "NOT IN" in s,
        "shared_clusters": lambda s: s.startswith("SELECT COUNT(*)")
        and "FROM concept_clusters" in s and "NOT IN" in s,
        "shared_embeddings": lambda s: s.startswith("SELECT COUNT(*)")
        and "FROM knowledge_embeddings" in s and "NOT IN" in s,
        "shared_relation_vectors": lambda s: s.startswith("SELECT COUNT(*)")
        and "FROM relation_embeddings" in s and "NOT IN" in s,
        "shared_reviews": lambda s: s.startswith(
            "SELECT id, review_status FROM knowledge_relations"),
    }
    plans: dict[str, list[str]] = {name: [] for name in roles}
    with postgres_database.connect() as connection:
        for statement, params in log:
            for name, matches in roles.items():
                if matches(statement):
                    plans[name].append(
                        _plan(connection, statement, params, capabilities_off)
                    )
    return plans


def test_memory_exclusion_statements_keep_their_index_paths(postgres_database):
    plans = _captured_plans(postgres_database)
    if os.environ.get("E46_PRINT_PLANS"):
        for name, captured in plans.items():
            print("\n=====", name)
            print(captured[0])
    for name, captured in plans.items():
        assert captured, f"{name}: the store issued no such statement"
        for plan in captured:
            assert "Seq Scan" not in plan, (name, plan)
            assert _no_per_row_subplan(plan), (name, plan)

    # The keyset-paged reads keep walking their key index (a LIMIT makes the
    # planner keep the outer order). Their Memory exclusion is ONE hashed
    # SubPlan per statement -- this notebook's Memory source ids, read through
    # the (notebook_id, source_type) index into a hash, probed per row: no join
    # with ``sources`` (the correlated anti join measured 5-10x per page).
    for name, ordered_by in (
        ("objects_page", "uq_knowledge_objects_ordinal"),
        ("objects_next_page", "uq_knowledge_objects_ordinal"),
        ("graph_objects", "uq_knowledge_objects_ordinal"),
        ("graph_relations", "pk_knowledge_relations"),
        ("relations_rows", "idx_knowledge_relations_nb"),
    ):
        plan = plans[name][0]
        assert ordered_by in plan, (name, plan)
        assert "Join" not in plan and "hashed SubPlan" in plan, (name, plan)
        assert _sources_bounded_by_notebook(plan), (name, plan)
    assert "Index Cond: (ordinal >" in plans["objects_next_page"][0]

    # the fold's delta batch lists only non-Memory sources by construction
    assert " on sources" not in plans["delta_relation_ids"][0]

    # The Memory id reads are driven by THIS notebook's Memory source ids: the
    # scan of ``sources`` is bounded by ``notebook_id`` (the seed holds another
    # notebook's Memory, so a deployment-wide Memory set would be visible here).
    for name in ("cluster_canonicals", "object_ids", "relation_ids"):
        assert _sources_bounded_by_notebook(plans[name][0]), (name, plans[name][0])

    # The cluster page carries NO Memory fragment (measured 200x slower with
    # one); it stays the index-only scan of the published generation. The
    # clusters holding a Memory member are read once, driven from the Memory
    # source set through the object and member indexes.
    cluster = plans["graph_clusters"][0]
    assert "memory" not in cluster and "Anti Join" not in cluster, cluster
    assert "Index Only Scan using idx_clusters_nb_canonical_member_gen" in cluster, cluster
    canonicals = plans["cluster_canonicals"][0]
    # driven from the Memory source set, then the object-by-source index (the
    # planner alternates between the covering and the plain one as statistics
    # move under load), then the member index: never a scan of the objects
    assert "idx_sources_nb_hidden_type" in canonicals, canonicals
    assert "idx_clusters_member" in canonicals, canonicals
    assert ("idx_knowledge_objects_source_id" in canonicals
            or "idx_knowledge_objects_source " in canonicals), canonicals

    # the probe and the Memory source read use the (notebook_id, source_type) index
    for name in ("has_memory", "memory_source_ids"):
        assert "idx_sources_nb_hidden_type" in plans[name][0], (name, plans[name][0])

    # shared_content_digest (build / fold / re-stamp only): each aggregate is
    # ONE hashed SubPlan over THIS notebook's Memory, no join, no every-
    # notebook Memory scan
    for name in ("shared_objects", "shared_relations", "shared_clusters",
                 "shared_embeddings", "shared_relation_vectors", "shared_reviews"):
        plan = plans[name][0]
        assert "hashed SubPlan" in plan and "Join" not in plan, (name, plan)
        assert _sources_bounded_by_notebook(plan), (name, plan)


def test_memory_exclusion_statements_have_no_correlated_lookup_under_the_real_planner(
    postgres_database,
):
    """The same statements, ANALYZEd data, NO capability switches: whatever the
    planner picks on its own costs, no statement may fall back to a correlated
    SubPlan per row or join ``sources`` per row, and every Memory set it reads
    is this notebook's."""
    plans = _captured_plans(postgres_database, capabilities_off=False)
    for name, captured in plans.items():
        assert captured, f"{name}: the store issued no such statement"
        for plan in captured:
            assert _no_per_row_subplan(plan), (name, plan)
    for name in ("objects_page", "objects_next_page", "graph_objects", "graph_relations",
                 "relations_rows"):
        assert "Join" not in plans[name][0], (name, plans[name][0])
    for name in ("delta_relation_ids", "graph_clusters"):
        assert " on sources" not in plans[name][0], (name, plans[name][0])
    # whatever access path the planner picks for ``sources``, it is bounded by
    # this notebook -- never every notebook's Memory
    for name in ("objects_page", "graph_objects", "graph_relations", "relations_rows",
                 "cluster_canonicals", "object_ids", "relation_ids",
                 "shared_objects", "shared_relations", "shared_clusters",
                 "shared_embeddings"):
        assert _sources_bounded_by_notebook(plans[name][0]), (name, plans[name][0])
