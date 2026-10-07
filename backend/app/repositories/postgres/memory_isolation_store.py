"""The ruling-M1 isolation marker, PostgreSQL side (SQLite twin:
``app/repositories/sqlite/memory_isolation_store.py``; the two must change
together).

``unified_kg_state.memory_isolation_version`` has three values, written by
migration ``0067_memory_kg_isolation.sql`` and moved on by this module only:

* **0** -- the notebook held a Memory source when the migration ran (the
  affected set F), or the post-readiness check below found a signal: its
  shared derived graph was built before Memory-derived objects were excluded
  from it; the migration removed the rows it could identify, and the notebook
  waits for one isolated rebuild (``app/services/memory_isolation_rebuild.py``)
  before anything may serve its derived artifacts again.
* **2** -- every other notebook with clusters (G, public libraries and copies
  included; a copy gets a state row of table defaults): not yet checked. The
  worker's :meth:`MemoryIsolationStore.seed_check_signal` decides, in bounded
  pages: a signal -> 0 (:meth:`queue_for_rebuild`, with the migration's reset,
  and :meth:`purge_stale_merge_candidates`), none -> 1.
* **1** -- isolated. The column default: every row created after the
  migration, and every notebook without clusters.

Writers of 1: the worker's :meth:`mark_isolated` (after a successful isolated
rebuild) and :meth:`mark_seed_checked`; and, with E4-2 in the same PR, the
rebuild end-write ``unified_kg_store.finish_rebuild_state`` (any successful
rebuild after the upgrade is isolated by construction, so a manual 刷新图谱
clears it too). Readers that gate old derived artifacts read ``<> 1``.

"Pending" = marker 0 on a LIVE notebook (``NOTEBOOK_LIVE_SQL``): a notebook
being deleted, copied into or imported is not rebuilt here and is not counted
as outstanding work; the same holds for marker 2.

It also holds checkup H10's read-only count: generic-path promotion proposals
of Memory-derived objects that were APPROVED before the write-side guard
existed (their Memory payload and raw evidence were copied into a public
library; what to do with those public objects is the deployment owner's
decision, so nothing here or in the migration rewrites them), and the
read-only census an operator runs on a production snapshot BEFORE deploying
(``scripts/memory_isolation_census.py``: :func:`read_only_connection` +
:meth:`MemoryIsolationStore.seed_check_census`).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

from app.repositories.postgres._store_utils import execute_many
from app.repositories.postgres.access_sql import NOTEBOOK_LIVE_SQL
from app.repositories.postgres.memory_sql import (
    cluster_seed_object_id,
    memory_derived_object,
    memory_source_type_predicate,
)

# promotion_candidates.object_type of the creator-only Memory promotion path
# (propose_memory_promotion): a promotion kind, not a source type.
_MEMORY_PROMOTION_KIND = "memory"
_APPROVED_MEMORY_PROMOTIONS_SQL = (
    "SELECT COUNT(*) AS n FROM promotion_candidates pc "
    "WHERE pc.notebook_id = %s AND pc.status = 'approved' "
    "AND pc.object_type <> %s "
    "AND EXISTS (SELECT 1 FROM knowledge_objects ko "
    "WHERE ko.id = pc.object_id AND ko.notebook_id = pc.notebook_id "
    f"AND {memory_derived_object('ko')})"
)

# unified_kg_state has no ``status`` column, so the unqualified live predicate
# resolves to notebooks.status.
_PENDING_FROM = (
    "FROM unified_kg_state u JOIN notebooks ON notebooks.id = u.notebook_id "
    f"WHERE u.memory_isolation_version = 0 AND {NOTEBOOK_LIVE_SQL}"
)


_SEED_CHECK_FROM = (
    "FROM unified_kg_state u JOIN notebooks ON notebooks.id = u.notebook_id "
    f"WHERE u.memory_isolation_version = 2 AND {NOTEBOOK_LIVE_SQL}"
)
# The same set BEFORE the upgrade (census on a v66 snapshot): live notebooks
# with clusters that hold no Memory source -- 0067's G.
_PRE_UPGRADE_SEED_CHECK_SQL = (
    "SELECT notebooks.id AS notebook_id FROM notebooks "
    f"WHERE {NOTEBOOK_LIVE_SQL} "
    "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.notebook_id = notebooks.id "
    f"AND {memory_source_type_predicate('s.source_type')}) "
    "AND EXISTS (SELECT 1 FROM concept_clusters cc WHERE cc.notebook_id = notebooks.id) "
    "ORDER BY notebooks.id"
)
# The notebooks holding a Memory source BEFORE the upgrade (0067's F, live).
_PRE_UPGRADE_F_SQL = (
    "SELECT notebooks.id AS notebook_id FROM notebooks "
    f"WHERE {NOTEBOOK_LIVE_SQL} "
    "AND EXISTS (SELECT 1 FROM sources s WHERE s.notebook_id = notebooks.id "
    f"AND {memory_source_type_predicate('s.source_type')}) "
    "ORDER BY notebooks.id"
)
_SEED = cluster_seed_object_id("cc")
# One page of a notebook's published clusters (canonical ids in (after, upto])
# holding a dangling seed: a degenerate-name canonical id whose object is gone,
# or a canonical name no live member carries (0067's G rationale).
_DANGLING_PAGE_SQL = (
    "SELECT 1 FROM ("
    "SELECT MAX(CASE WHEN ko.id IS NOT NULL "
    "AND COALESCE(ko.payload ->> 'name', '') = cc.canonical_name "
    "THEN 1 ELSE 0 END) AS anchored, "
    f"MAX(CASE WHEN {_SEED} IS NOT NULL AND NOT EXISTS ("
    f"SELECT 1 FROM knowledge_objects sk WHERE sk.id = {_SEED}) "
    "THEN 1 ELSE 0 END) AS lost_seed "
    "FROM concept_clusters cc "
    "LEFT JOIN knowledge_objects ko ON ko.id = cc.member_object_id "
    "WHERE cc.notebook_id = %s AND cc.generation = %s "
    "AND cc.canonical_id > %s AND cc.canonical_id <= %s "
    "GROUP BY cc.canonical_id) d "
    "WHERE d.anchored = 0 OR d.lost_seed = 1 LIMIT 1"
)
_CANONICAL_PAGE_SQL = (
    "SELECT DISTINCT cc.canonical_id AS k FROM concept_clusters cc "
    "WHERE cc.notebook_id = %s AND cc.generation = %s AND cc.canonical_id > %s "
    "ORDER BY cc.canonical_id LIMIT %s"
)
# Stale references (pk_mention_edges / idx_commmem_nb_can, then one key probe
# per row of the page, stopping at the first hit): a mention row whose claim
# object is gone; a community member naming neither a cluster of the notebook
# nor an object. ``OFFSET 0`` keeps each NOT EXISTS a per-row probe (a
# correlated SubPlan): without it the planner may turn the anti-join into a
# hash join that reads the whole probed table, which is cheaper than 5000
# probes only while that table is small (SQLite has no such rewrite and no
# bare OFFSET, so its twin omits it).
_MENTION_PAGE_SQL = (
    "SELECT DISTINCT me.claim_object_id AS k FROM mention_edges me "
    "WHERE me.notebook_id = %s AND me.claim_object_id > %s "
    "ORDER BY me.claim_object_id LIMIT %s"
)
_STALE_MENTION_PAGE_SQL = (
    "SELECT 1 FROM mention_edges me "
    "WHERE me.notebook_id = %s AND me.claim_object_id > %s "
    "AND me.claim_object_id <= %s "
    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko WHERE ko.id = me.claim_object_id "
    "OFFSET 0) "
    "LIMIT 1"
)
_MEMBER_PAGE_SQL = (
    "SELECT DISTINCT cm.canonical_id AS k FROM community_members cm "
    "WHERE cm.notebook_id = %s AND cm.generation = %s AND cm.canonical_id > %s "
    "ORDER BY cm.canonical_id LIMIT %s"
)
_STALE_MEMBER_PAGE_SQL = (
    "SELECT 1 FROM community_members cm "
    "WHERE cm.notebook_id = %s AND cm.generation = %s "
    "AND cm.canonical_id > %s AND cm.canonical_id <= %s "
    "AND NOT EXISTS (SELECT 1 FROM concept_clusters cc "
    "WHERE cc.notebook_id = cm.notebook_id AND cc.canonical_id = cm.canonical_id "
    "OFFSET 0) "
    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko WHERE ko.id = cm.canonical_id "
    "OFFSET 0) "
    "LIMIT 1"
)


def _seed_object_gone(column: str) -> str:
    seed = cluster_seed_object_id("m", column)
    return (
        f"({seed} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM knowledge_objects o "
        f"WHERE o.id = {seed} OFFSET 0))"
    )


# Only a SENTINEL side (``<prefix>~<object id>``, decoded by
# ``memory_sql.cluster_seed_object_id(alias, column)``) whose object no longer
# exists is provably dead. A canonical id carried by no cluster row is NOT:
# canonical ids drift when a cluster's min member changes, so a decided pair's
# losing side is normally carried by nobody (decisions key on seeds --
# ``decided_seed_pairs_from``), and deleting it would undo a curator's merge or
# rejection. A bridge-shaped decision naming a since-deleted Memory's name is
# kept: it acts only if some object mints that seed again, and it holds a
# normalised name, nothing else.
_PURGE_STALE_MERGE_SQL = (
    "DELETE FROM concept_merge_candidates m WHERE m.notebook_id = %s "
    f"AND ({_seed_object_gone('canonical_a')} OR {_seed_object_gone('canonical_b')})"
)
# The census' per-notebook facts: the copy-bound facts the service judges
# copyability on (``load_notebook_scale_facts``) and the merge-review pairs
# of the last rebuild (its kg_rebuild_checkpoint rows). One notebook-prefixed
# index read each.
_CENSUS_FACTS_SQL = {
    "objects": "SELECT COUNT(*) AS value FROM knowledge_objects WHERE notebook_id = %s",
    "chunks": "SELECT COUNT(*) AS value FROM chunks WHERE notebook_id = %s",
    "bytes": ("SELECT COALESCE(SUM(file_size), 0) AS value FROM sources "
              "WHERE notebook_id = %s"),
    "merge_review_pairs": ("SELECT COUNT(*) AS value FROM kg_rebuild_checkpoint "
                           "WHERE notebook_id = %s AND stage = 'merge_review'"),
}
SEED_CHECK_PAGE = 5000


def _paged_probe(db: Any, ids_sql: str, probe_sql: str, notebook_id: str,
                 page_size: int, *bound: Any) -> bool:
    """Walk a notebook's keys in pages of ``page_size`` (``ids_sql``) and run
    ``probe_sql`` over each page's key range (after, last]; True at the first
    hit. Every statement is bounded by its page."""
    after = ""
    while True:
        keys = [r["k"] for r in db.execute(
            ids_sql, (notebook_id, *bound, after, page_size)).fetchall()]
        if not keys:
            return False
        if db.execute(probe_sql, (notebook_id, *bound, after, keys[-1])).fetchone():
            return True
        after = keys[-1]


class _CountingConnection:
    """Counts the statements the census sends (read-only diagnostics)."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.statements = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.statements += 1
        return self._db.execute(*args, **kwargs)


@contextmanager
def read_only_connection(database_url: str) -> Iterator[Any]:
    """A connection that cannot write (``default_transaction_read_only``),
    rolled back and closed on exit -- for the pre-deploy census."""
    import psycopg
    from psycopg.rows import dict_row

    from psycopg.conninfo import conninfo_to_dict

    # append to the URL's own options (a search_path, say), never replace them
    options = conninfo_to_dict(database_url).get("options") or ""
    conn = psycopg.connect(
        database_url, row_factory=dict_row,
        options=f"{options} -c default_transaction_read_only=on".strip(),
    )
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


class MemoryIsolationStore:
    # ---------------------------------------- post-readiness check (marker 2)
    @staticmethod
    def seed_check_notebook_ids(db: Any) -> List[str]:
        """Live notebooks the migration marked 2 (outside F, with clusters,
        public libraries and copies included): awaiting the check."""
        rows = db.execute(
            f"SELECT u.notebook_id {_SEED_CHECK_FROM} ORDER BY u.notebook_id"
        ).fetchall()
        return [row["notebook_id"] for row in rows]

    @staticmethod
    def seed_check_count(db: Any) -> int:
        row = db.execute(f"SELECT COUNT(*) AS n {_SEED_CHECK_FROM}").fetchone()
        return int(row["n"])

    @staticmethod
    def seed_check_signal(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> Optional[str]:
        """Why a marker-2 notebook must be rebuilt, cheapest signal first;
        None = clean. A deleted Memory leaves no record of its own, so each
        signal is a trace it can leave behind:

        * ``"dirty"`` -- ``unified_kg_state.dirty`` (primary key): the graph
          changed since its last rebuild (deleting a Memory source marks it;
          only a rebuild clears it), so its descriptions and communities may
          still carry text written while that Memory was a member;
        * ``"seed"`` -- :meth:`has_dangling_seed`;
        * ``"stale_reference"`` -- :meth:`has_stale_reference`."""
        row = db.execute(
            "SELECT dirty FROM unified_kg_state WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchone()
        if row is not None and int(row["dirty"]):
            return "dirty"
        if MemoryIsolationStore.has_dangling_seed(db, notebook_id, page_size):
            return "seed"
        if MemoryIsolationStore.has_stale_reference(db, notebook_id, page_size):
            return "stale_reference"
        return None

    @staticmethod
    def has_dangling_seed(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> bool:
        """Whether a PUBLISHED cluster of the notebook was seeded by an object
        that no longer exists. Pages of ``page_size`` canonical ids in order
        (idx_clusters_nb_canonical_member), each statement bounded by its
        page, stopping at the first dangling cluster."""
        row = db.execute(
            "SELECT COALESCE(cluster_generation, 0) AS g FROM unified_kg_state "
            "WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        generation = int(row["g"]) if row is not None else 0
        return _paged_probe(db, _CANONICAL_PAGE_SQL, _DANGLING_PAGE_SQL,
                            notebook_id, page_size, generation)

    @staticmethod
    def has_stale_reference(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> bool:
        """Whether a mention row names a claim object that no longer exists,
        or a member of a PUBLISHED community names neither a cluster of the notebook (any
        generation) nor an object -- rows derived while a since-deleted
        object (a Memory) was part of the graph. Paged like
        :meth:`has_dangling_seed`."""
        if _paged_probe(db, _MENTION_PAGE_SQL, _STALE_MENTION_PAGE_SQL,
                        notebook_id, page_size):
            return True
        row = db.execute(
            "SELECT COALESCE(community_generation, 0) AS g FROM unified_kg_state "
            "WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        generation = int(row["g"]) if row is not None else 0
        return _paged_probe(db, _MEMBER_PAGE_SQL, _STALE_MEMBER_PAGE_SQL,
                            notebook_id, page_size, generation)

    @staticmethod
    def queue_for_rebuild(db: Any, notebook_id: str) -> bool:
        """A marker-2 notebook with a signal: the same one-time reset the
        migration gives F (marker 0, derived-layer sequences -1, dirty, both
        mutation counters +1 -- kg_mutation_seq is part of the cluster input
        version, so the rebuild re-clusters)."""
        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 0, "
            "community_seq = -1, canonical_rel_seq = -1, mention_seq = -1, "
            "dirty = 1, kg_mutation_seq = kg_mutation_seq + 1, "
            "cluster_mutation_seq = cluster_mutation_seq + 1, updated_at = now() "
            "WHERE notebook_id = %s AND memory_isolation_version = 2",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def purge_stale_merge_candidates(db: Any, notebook_id: str) -> int:
        """Merge candidates (any status) of a queued notebook with a SENTINEL
        side minted from an object that no longer exists (a since-deleted
        Memory's degenerate-name seed): provably dead, and its rationale may
        quote the Memory. Nothing else is touched -- see
        ``_PURGE_STALE_MERGE_SQL`` for why a side carried by no cluster row is
        a curator's decision, not a stale one. ``idx_candidates_nb_status``,
        then one primary-key probe of knowledge_objects per sentinel side."""
        cursor = db.execute(_PURGE_STALE_MERGE_SQL, (notebook_id,))
        return int(cursor.rowcount or 0)

    @staticmethod
    def mark_seed_checked(db: Any, notebook_id: str) -> bool:
        """A marker-2 notebook without any signal: isolated (1), nothing else
        moves."""
        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 1 "
            "WHERE notebook_id = %s AND memory_isolation_version = 2",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def not_isolated(db: Any, notebook_id: str) -> bool:
        """Checkup H9: the marker is not 1 (0 awaiting the rebuild, 2 awaiting
        the post-readiness check). A missing state row is isolated (the
        migration gave every notebook with clusters a row)."""
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchone()
        return row is not None and int(row["memory_isolation_version"]) != 1

    # ------------------------------------------------ pre-deploy census
    @staticmethod
    def seed_check_signals(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> dict:
        """Every signal of :meth:`seed_check_signal`, each judged on its own
        (no short circuit) -- for the census only."""
        row = db.execute(
            "SELECT dirty FROM unified_kg_state WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchone()
        return {
            "dirty": row is not None and bool(int(row["dirty"])),
            "seed": MemoryIsolationStore.has_dangling_seed(db, notebook_id, page_size),
            "stale_reference": MemoryIsolationStore.has_stale_reference(
                db, notebook_id, page_size),
        }

    @staticmethod
    def census_facts(db: Any, notebook_id: str) -> dict:
        """The copy-bound facts (``load_notebook_scale_facts``' bytes / chunks
        / objects) and the merge-review pair estimate of one notebook."""
        def one(sql: str) -> int:
            return int(db.execute(sql, (notebook_id,)).fetchone()["value"])

        return {name: one(sql) for name, sql in _CENSUS_FACTS_SQL.items()}

    @staticmethod
    def seed_check_census(
        db: Any, notebook_ids: Optional[List[str]] = None,
        page_size: int = SEED_CHECK_PAGE, all_signals: bool = False,
    ) -> dict:
        """Read-only: what the post-readiness pass will do, notebook by notebook,
        with the cost of each decision -- for an operator on a production
        snapshot BEFORE deploying. Rows of set ``"F"``: notebooks holding a
        Memory source (always rebuilt; ``signal`` = ``"memory"``). Rows of set
        ``"G"``: every other notebook with clusters, with the signal that
        queues it (None = clean, marked isolated without a rebuild) and the
        seconds / statements of that check; with ``all_signals`` also every
        signal judged on its own (no short circuit), so "dirty only" can be
        told apart. Each row carries ``objects`` / ``chunks`` / ``bytes`` (the
        copy-bound facts) and ``merge_review_pairs``: the ambiguous seed pairs
        the notebook's last rebuild sent to the model (its kg_rebuild_checkpoint
        rows of stage merge_review), the estimate of what one more rebuild
        sends. On a database without the marker column (a pre-upgrade
        snapshot) F and G are computed live as 0067 would; after the upgrade
        they are markers 0 and 2. ``notebook_ids`` restricts both. Writes
        nothing."""
        upgraded = db.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'unified_kg_state' "
            "AND column_name = 'memory_isolation_version'"
        ).fetchone() is not None
        if upgraded:
            f_ids = MemoryIsolationStore.pending_notebook_ids(db)
            g_ids = MemoryIsolationStore.seed_check_notebook_ids(db)
        else:
            f_ids = [r["notebook_id"] for r in db.execute(_PRE_UPGRADE_F_SQL).fetchall()]
            g_ids = [r["notebook_id"] for r in db.execute(
                _PRE_UPGRADE_SEED_CHECK_SQL).fetchall()]
        if notebook_ids is not None:
            wanted = set(notebook_ids)
            f_ids = [i for i in f_ids if i in wanted]
            g_ids = [i for i in g_ids if i in wanted]
        rows = []
        for notebook_id in f_ids:
            rows.append({
                "notebook_id": notebook_id, "set": "F", "signal": "memory",
                "signals": None, "seconds": 0.0, "statements": 0,
                **MemoryIsolationStore.census_facts(db, notebook_id),
            })
        for notebook_id in g_ids:
            counting = _CountingConnection(db)
            started = time.perf_counter()
            signal = MemoryIsolationStore.seed_check_signal(
                counting, notebook_id, page_size)
            seconds = time.perf_counter() - started
            rows.append({
                "notebook_id": notebook_id, "set": "G", "signal": signal,
                "signals": (MemoryIsolationStore.seed_check_signals(
                    db, notebook_id, page_size) if all_signals else None),
                "seconds": seconds, "statements": counting.statements,
                **MemoryIsolationStore.census_facts(db, notebook_id),
            })
        return {"phase": "post-upgrade" if upgraded else "pre-upgrade",
                "notebooks": rows}

    # ------------------------------------------------ isolated rebuild (0)
    @staticmethod
    def pending_notebook_ids(db: Any) -> List[str]:
        """Live notebooks still awaiting the isolation rebuild, in id order
        (a deterministic work order for the single rebuild worker)."""
        rows = db.execute(
            f"SELECT u.notebook_id {_PENDING_FROM} ORDER BY u.notebook_id"
        ).fetchall()
        return [row["notebook_id"] for row in rows]

    @staticmethod
    def pending_count(db: Any) -> int:
        row = db.execute(f"SELECT COUNT(*) AS n {_PENDING_FROM}").fetchone()
        return int(row["n"])

    @staticmethod
    def is_pending(db: Any, notebook_id: str) -> bool:
        """Primary-key read of one notebook's marker (False when the row is
        missing: the migration gave every notebook of F and G a row, so a
        missing one was never queued)."""
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchone()
        return row is not None and int(row["memory_isolation_version"]) == 0

    @staticmethod
    def forget_cluster_input_version(db: Any, notebook_id: str) -> None:
        """Make the next rebuild of a notebook awaiting its isolated rebuild
        (marker 0) a real one: an empty stored input version never matches,
        so ``rebuild_unified_kg(force=False)`` cannot take its skip path --
        which would set no end-state totals and leave the pre-isolation ones,
        Memory-derived rows counted, in ``unified_kg_status``.  One
        primary-key UPDATE; a no-op once the notebook is isolated."""
        db.execute(
            "UPDATE unified_kg_state SET cluster_input_version = '' "
            "WHERE notebook_id = %s AND memory_isolation_version = 0",
            (notebook_id,),
        )

    @staticmethod
    def mark_isolated(db: Any, notebook_id: str) -> bool:
        """Set the marker to 1 after a successful isolated rebuild. True when
        this call changed it (idempotent: an already-isolated notebook is a
        no-op)."""
        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 1 "
            "WHERE notebook_id = %s AND memory_isolation_version = 0",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def memory_objects(db: Any, notebook_id: str) -> List[dict]:
        """``[{object_id, object_type, name}]`` of the notebook's Memory-derived
        objects, reached from its Memory sources (``idx_sources_nb_hidden_type``
        then ``idx_knowledge_objects_source``). Input of
        ``kg_merge.purge_bridge_canonical_ids`` for the bridge-candidate purge."""
        rows = db.execute(
            "SELECT ko.id AS object_id, ko.object_type, "
            "ko.payload ->> 'name' AS name "
            "FROM sources s JOIN knowledge_objects ko ON ko.source_id = s.id "
            f"WHERE s.notebook_id = %s AND {memory_source_type_predicate('s.source_type')} "
            "AND ko.notebook_id = %s ORDER BY ko.id",
            (notebook_id, notebook_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def purge_bridge_candidates(
        db: Any, notebook_id: str, bridge_ids: List[str]
    ) -> int:
        """Delete the merge candidates (any status) of ``notebook_id`` that
        name one of ``bridge_ids`` -- the bridge canonical ids of the
        notebook's Memory concepts, which only ``kg_merge``'s normaliser can
        derive -- where no cluster row carries that id (a live shared cluster
        that happens to carry the same seed keeps its candidates). The ids go
        through a transaction-local temp table, never one bound list."""
        ids = sorted(set(bridge_ids))
        if not ids:
            return 0
        db.execute(
            'CREATE TEMP TABLE IF NOT EXISTS mib_bridge (id text COLLATE "C" '
            "PRIMARY KEY) ON COMMIT DROP"
        )
        db.execute("DELETE FROM mib_bridge")
        execute_many(db, "INSERT INTO mib_bridge (id) VALUES (%s)", [(i,) for i in ids])
        cursor = db.execute(
            "DELETE FROM concept_merge_candidates m WHERE m.notebook_id = %s AND ("
            "(m.canonical_a IN (SELECT id FROM mib_bridge) AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xa WHERE xa.notebook_id = m.notebook_id "
            "AND xa.canonical_id = m.canonical_a)) OR "
            "(m.canonical_b IN (SELECT id FROM mib_bridge) AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xb WHERE xb.notebook_id = m.notebook_id "
            "AND xb.canonical_id = m.canonical_b)))",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def approved_memory_promotion_count(db: Any, notebook_id: str) -> int:
        """Checkup H10: approved generic-path promotions of this notebook's
        Memory-derived objects (``idx_promotion_nb (notebook_id, status)``,
        then one primary-key probe per approved row)."""
        row = db.execute(
            _APPROVED_MEMORY_PROMOTIONS_SQL, (notebook_id, _MEMORY_PROMOTION_KIND)
        ).fetchone()
        return int(row["n"])
