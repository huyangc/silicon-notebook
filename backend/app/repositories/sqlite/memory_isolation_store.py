"""The ruling-M1 isolation marker, SQLite side (PostgreSQL twin:
``app/repositories/postgres/memory_isolation_store.py``; the two must change
together -- the full contract, including the three marker values 0 / 1 / 2
and who writes each, is written in the PostgreSQL module's docstring).

``unified_kg_state.memory_isolation_version`` is written by ``_migration_87``
(PostgreSQL ``0067_memory_kg_isolation.sql``): 0 = awaiting the isolated
rebuild of ``app/services/memory_isolation_rebuild.py`` (the notebooks that
held a Memory source, and those the post-readiness check queued), 2 =
awaiting that check (every other notebook with clusters), 1 = isolated (the
default). "Pending" = marker 0 on a LIVE notebook (``NOTEBOOK_LIVE_SQL``).
Also checkup H10's read-only count of approved generic-path promotions of
Memory-derived objects, and the read-only pre-deploy census.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional
from urllib.parse import quote

from app.repositories.sqlite.access_sql import NOTEBOOK_LIVE_SQL
from app.repositories.sqlite.memory_sql import (
    cluster_seed_object_id,
    memory_derived_object,
    memory_source_type_predicate,
)

_MEMORY_PROMOTION_KIND = "memory"
_APPROVED_MEMORY_PROMOTIONS_SQL = (
    "SELECT COUNT(*) AS n FROM promotion_candidates pc "
    "WHERE pc.notebook_id = ? AND pc.status = 'approved' "
    "AND pc.object_type <> ? "
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
_DANGLING_PAGE_SQL = (
    "SELECT 1 FROM ("
    "SELECT MAX(CASE WHEN ko.id IS NOT NULL AND COALESCE(CASE WHEN "
    "json_valid(ko.payload) AND json_type(ko.payload) = 'object' "
    "THEN json_extract(ko.payload, '$.name') END, '') = cc.canonical_name "
    "THEN 1 ELSE 0 END) AS anchored, "
    f"MAX(CASE WHEN {_SEED} IS NOT NULL AND NOT EXISTS ("
    f"SELECT 1 FROM knowledge_objects sk WHERE sk.id = {_SEED}) "
    "THEN 1 ELSE 0 END) AS lost_seed "
    "FROM concept_clusters cc "
    "LEFT JOIN knowledge_objects ko ON ko.id = cc.member_object_id "
    "WHERE cc.notebook_id = ? AND cc.generation = ? "
    "AND cc.canonical_id > ? AND cc.canonical_id <= ? "
    "GROUP BY cc.canonical_id) d "
    "WHERE d.anchored = 0 OR d.lost_seed = 1 LIMIT 1"
)
_CANONICAL_PAGE_SQL = (
    "SELECT DISTINCT cc.canonical_id AS k FROM concept_clusters cc "
    "WHERE cc.notebook_id = ? AND cc.generation = ? AND cc.canonical_id > ? "
    "ORDER BY cc.canonical_id LIMIT ?"
)
_MENTION_PAGE_SQL = (
    "SELECT DISTINCT me.claim_object_id AS k FROM mention_edges me "
    "WHERE me.notebook_id = ? AND me.claim_object_id > ? "
    "ORDER BY me.claim_object_id LIMIT ?"
)
_STALE_MENTION_PAGE_SQL = (
    "SELECT 1 FROM mention_edges me "
    "WHERE me.notebook_id = ? AND me.claim_object_id > ? "
    "AND me.claim_object_id <= ? "
    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko WHERE ko.id = me.claim_object_id) "
    "LIMIT 1"
)
_MEMBER_PAGE_SQL = (
    "SELECT DISTINCT cm.canonical_id AS k FROM community_members cm "
    "WHERE cm.notebook_id = ? AND cm.generation = ? AND cm.canonical_id > ? "
    "ORDER BY cm.canonical_id LIMIT ?"
)
_STALE_MEMBER_PAGE_SQL = (
    "SELECT 1 FROM community_members cm "
    "WHERE cm.notebook_id = ? AND cm.generation = ? "
    "AND cm.canonical_id > ? AND cm.canonical_id <= ? "
    "AND NOT EXISTS (SELECT 1 FROM concept_clusters cc "
    "WHERE cc.notebook_id = cm.notebook_id AND cc.canonical_id = cm.canonical_id) "
    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko WHERE ko.id = cm.canonical_id) "
    "LIMIT 1"
)


def _seed_object_gone(column: str) -> str:
    seed = cluster_seed_object_id("concept_merge_candidates", column)
    return (
        f"({seed} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM knowledge_objects o "
        f"WHERE o.id = {seed}))"
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
    "DELETE FROM concept_merge_candidates WHERE notebook_id = ? "
    f"AND ({_seed_object_gone('canonical_a')} OR {_seed_object_gone('canonical_b')})"
)
# The census' per-notebook facts: the copy-bound facts the service judges
# copyability on (``load_notebook_scale_facts``) and the merge-review pairs
# of the last rebuild (its kg_rebuild_checkpoint rows). One notebook-prefixed
# index read each.
_CENSUS_FACTS_SQL = {
    "objects": "SELECT COUNT(*) AS value FROM knowledge_objects WHERE notebook_id = ?",
    "chunks": "SELECT COUNT(*) AS value FROM chunks WHERE notebook_id = ?",
    "bytes": ("SELECT COALESCE(SUM(file_size), 0) AS value FROM sources "
              "WHERE notebook_id = ?"),
    "merge_review_pairs": ("SELECT COUNT(*) AS value FROM kg_rebuild_checkpoint "
                           "WHERE notebook_id = ? AND stage = 'merge_review'"),
}
SEED_CHECK_PAGE = 5000


def _paged_probe(db: Any, ids_sql: str, probe_sql: str, notebook_id: str,
                 page_size: int, *bound: Any) -> bool:
    """See the PostgreSQL twin."""
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
def read_only_connection(database_url: str) -> Iterator[sqlite3.Connection]:
    """``mode=ro`` plus ``query_only``: the pre-deploy census cannot write
    (a WAL database also needs its ``-shm`` file present, as for every
    read-only open)."""
    from app.core.database_url import database_identity

    path = Path(database_identity(database_url).database).resolve()
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True,
                           timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


class MemoryIsolationStore:
    # ---------------------------------------- post-readiness check (marker 2)
    @staticmethod
    def seed_check_notebook_ids(db: Any) -> List[str]:
        """See the PostgreSQL twin."""
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
        """See the PostgreSQL twin (``"dirty"`` / ``"seed"`` /
        ``"stale_reference"`` / None, cheapest first)."""
        row = db.execute(
            "SELECT dirty FROM unified_kg_state WHERE notebook_id = ?",
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
        """See the PostgreSQL twin (published generation, paged, stops at the
        first dangling cluster)."""
        row = db.execute(
            "SELECT COALESCE(cluster_generation, 0) AS g FROM unified_kg_state "
            "WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        generation = int(row["g"]) if row is not None else 0
        return _paged_probe(db, _CANONICAL_PAGE_SQL, _DANGLING_PAGE_SQL,
                            notebook_id, page_size, generation)

    @staticmethod
    def has_stale_reference(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> bool:
        """See the PostgreSQL twin."""
        if _paged_probe(db, _MENTION_PAGE_SQL, _STALE_MENTION_PAGE_SQL,
                        notebook_id, page_size):
            return True
        row = db.execute(
            "SELECT COALESCE(community_generation, 0) AS g FROM unified_kg_state "
            "WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        generation = int(row["g"]) if row is not None else 0
        return _paged_probe(db, _MEMBER_PAGE_SQL, _STALE_MEMBER_PAGE_SQL,
                            notebook_id, page_size, generation)

    @staticmethod
    def queue_for_rebuild(db: Any, notebook_id: str) -> bool:
        """See the PostgreSQL twin (the migration's one-time reset)."""
        from datetime import datetime, timezone

        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 0, "
            "community_seq = -1, canonical_rel_seq = -1, mention_seq = -1, "
            "dirty = 1, kg_mutation_seq = kg_mutation_seq + 1, "
            "cluster_mutation_seq = cluster_mutation_seq + 1, updated_at = ? "
            "WHERE notebook_id = ? AND memory_isolation_version = 2",
            (datetime.now(timezone.utc).isoformat(), notebook_id),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def purge_stale_merge_candidates(db: Any, notebook_id: str) -> int:
        """See the PostgreSQL twin (sentinel sides whose object is gone only)."""
        cursor = db.execute(_PURGE_STALE_MERGE_SQL, (notebook_id,))
        return int(cursor.rowcount or 0)

    @staticmethod
    def mark_seed_checked(db: Any, notebook_id: str) -> bool:
        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 1 "
            "WHERE notebook_id = ? AND memory_isolation_version = 2",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def not_isolated(db: Any, notebook_id: str) -> bool:
        """Checkup H9 (see the PostgreSQL twin)."""
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return row is not None and int(row["memory_isolation_version"]) != 1

    # ------------------------------------------------ pre-deploy census
    @staticmethod
    def seed_check_signals(
        db: Any, notebook_id: str, page_size: int = SEED_CHECK_PAGE
    ) -> dict:
        """See the PostgreSQL twin."""
        row = db.execute(
            "SELECT dirty FROM unified_kg_state WHERE notebook_id = ?",
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
        """See the PostgreSQL twin."""
        def one(sql: str) -> int:
            return int(db.execute(sql, (notebook_id,)).fetchone()["value"])

        return {name: one(sql) for name, sql in _CENSUS_FACTS_SQL.items()}

    @staticmethod
    def seed_check_census(
        db: Any, notebook_ids: Optional[List[str]] = None,
        page_size: int = SEED_CHECK_PAGE, all_signals: bool = False,
    ) -> dict:
        """See the PostgreSQL twin (read-only; F and G rows, facts, optional
        independent signals)."""
        upgraded = any(
            row["name"] == "memory_isolation_version"
            for row in db.execute("PRAGMA table_info(unified_kg_state)").fetchall()
        )
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
        """See the PostgreSQL twin (primary-key read; a missing row was never
        queued)."""
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return row is not None and int(row["memory_isolation_version"]) == 0

    @staticmethod
    def forget_cluster_input_version(db: Any, notebook_id: str) -> None:
        """See the PostgreSQL twin (the isolated rebuild never takes the
        skip path, so its end-state totals are the shared graph's)."""
        db.execute(
            "UPDATE unified_kg_state SET cluster_input_version = '' "
            "WHERE notebook_id = ? AND memory_isolation_version = 0",
            (notebook_id,),
        )

    @staticmethod
    def mark_isolated(db: Any, notebook_id: str) -> bool:
        """Set the marker to 1 after a successful isolated rebuild. True when
        this call changed it (idempotent)."""
        cursor = db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version = 1 "
            "WHERE notebook_id = ? AND memory_isolation_version = 0",
            (notebook_id,),
        )
        return int(cursor.rowcount or 0) > 0

    @staticmethod
    def memory_objects(db: Any, notebook_id: str) -> List[dict]:
        """See the PostgreSQL twin (``{object_id, object_type, name}`` of the
        notebook's Memory-derived objects, from its Memory sources)."""
        rows = db.execute(
            "SELECT ko.id AS object_id, ko.object_type AS object_type, "
            "CASE WHEN json_valid(ko.payload) AND json_type(ko.payload) = 'object' "
            "THEN json_extract(ko.payload, '$.name') END AS name "
            "FROM sources s JOIN knowledge_objects ko ON ko.source_id = s.id "
            f"WHERE s.notebook_id = ? AND {memory_source_type_predicate('s.source_type')} "
            "AND ko.notebook_id = ? ORDER BY ko.id",
            (notebook_id, notebook_id),
        ).fetchall()
        return [
            {"object_id": r["object_id"], "object_type": r["object_type"],
             "name": r["name"]}
            for r in rows
        ]

    @staticmethod
    def purge_bridge_candidates(
        db: Any, notebook_id: str, bridge_ids: List[str]
    ) -> int:
        """See the PostgreSQL twin (merge candidates naming a Memory concept's
        bridge id that no cluster row carries; ids through a temp table)."""
        ids = sorted(set(bridge_ids))
        if not ids:
            return 0
        db.execute("DROP TABLE IF EXISTS temp.mib_bridge")
        db.execute("CREATE TEMP TABLE mib_bridge (id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO mib_bridge (id) VALUES (?)", [(i,) for i in ids])
        cursor = db.execute(
            "DELETE FROM concept_merge_candidates WHERE notebook_id = ? AND ("
            "(canonical_a IN (SELECT id FROM mib_bridge) AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xa "
            "WHERE xa.notebook_id = concept_merge_candidates.notebook_id "
            "AND xa.canonical_id = concept_merge_candidates.canonical_a)) OR "
            "(canonical_b IN (SELECT id FROM mib_bridge) AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xb "
            "WHERE xb.notebook_id = concept_merge_candidates.notebook_id "
            "AND xb.canonical_id = concept_merge_candidates.canonical_b)))",
            (notebook_id,),
        )
        removed = int(cursor.rowcount or 0)
        db.execute("DROP TABLE IF EXISTS temp.mib_bridge")
        return removed

    @staticmethod
    def approved_memory_promotion_count(db: Any, notebook_id: str) -> int:
        """Checkup H10 (see the PostgreSQL twin's docstring)."""
        row = db.execute(
            _APPROVED_MEMORY_PROMOTIONS_SQL, (notebook_id, _MEMORY_PROMOTION_KIND)
        ).fetchone()
        return int(row["n"])
