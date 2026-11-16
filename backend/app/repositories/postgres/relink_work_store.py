"""Durable source-partition receipts for relink, owned by the caller's transaction."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps

from psycopg.errors import LockNotAvailable

from app.repositories.postgres.id_binding import bind_ids, execute_ids, member_of

_reserved_receipts: ContextVar[list | None] = ContextVar("reserved_relink_receipts", default=None)


def receipt_lock_scope(method):
    """Roll back speculative reservations on refusal; never publish placeholders."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        db = args[0] if hasattr(args[0], "execute") else args[1]
        reserved = []
        token = _reserved_receipts.set(reserved)
        db.execute("SAVEPOINT governance_receipt_scope")
        try:
            result = method(*args, **kwargs)
            for notebook_id, source_id in reserved:
                db.execute("DELETE FROM kg_relink_work WHERE notebook_id=%s AND source_id=%s "
                           "AND revision=0", (notebook_id, source_id))
            db.execute("RELEASE SAVEPOINT governance_receipt_scope")
            return result
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT governance_receipt_scope")
            db.execute("RELEASE SAVEPOINT governance_receipt_scope")
            raise
        finally:
            _reserved_receipts.reset(token)
    return wrapped


def reserve_for_objects(db, notebook_id, object_ids, *, adjacent=False, source_ids=(), wait=True):
    """Reserve partitions before object locks, without invalidating completed work.

    A revision-zero row reserves an absent key and is removed by the scope if
    no mutation enqueues it. Nonwaiting follow-up work only reserves existing
    rows: a missing key is deferred, since its insertion could wait on another
    publisher's uncommitted unique key.
    """
    ids = list(object_ids)
    sources = set(source_ids)
    if source_ids:
        owners = bind_ids(list(source_ids))
        for endpoint in ("source_object_id", "target_object_id"):
            sources.update(row["source_id"] for row in execute_ids(
                db, "SELECT COALESCE(o.source_id,'') AS source_id "
                "FROM knowledge_relations r JOIN knowledge_objects o "
                f"ON o.id=r.{endpoint} AND o.notebook_id=r.notebook_id "
                f"WHERE r.notebook_id=%s AND {member_of('r.source_id', owners)}",
                (notebook_id, owners.param),
            ).fetchall())
    if ids:
        bound = bind_ids(ids)
        query = ("SELECT COALESCE(source_id,'') AS source_id FROM knowledge_objects "
                 f"WHERE notebook_id=%s AND {member_of('id', bound)}")
        params = [notebook_id, bound.param]
        if adjacent:
            for endpoint, other in (("source_object_id", "target_object_id"),
                                    ("target_object_id", "source_object_id")):
                query += (" UNION SELECT COALESCE(o.source_id,'') AS source_id "
                          "FROM knowledge_relations r JOIN knowledge_objects o "
                          f"ON o.id=r.{other} AND o.notebook_id=r.notebook_id "
                          f"WHERE r.notebook_id=%s AND {member_of('r.' + endpoint, bound)}")
                params.extend((notebook_id, bound.param))
        sources.update(row["source_id"] for row in execute_ids(db, query, tuple(params)).fetchall())
    return _reserve_source_ids(db, notebook_id, sources, wait=wait)


def _reserve_source_ids(db, notebook_id, sources, *, wait=True):
    reserved = _reserved_receipts.get()
    if reserved is None:
        raise RuntimeError("receipt reservation requires receipt_lock_scope")
    db.execute("SAVEPOINT governance_receipt_reserve")
    created = []
    try:
        for source_id in sorted(sources):
            if wait:
                db.execute("INSERT INTO kg_relink_work "
                           "(notebook_id,source_id,algorithm_version,revision,status) "
                           "VALUES (%s,%s,0,0,'done') ON CONFLICT DO NOTHING",
                           (notebook_id, source_id))
            row = db.execute("SELECT revision FROM kg_relink_work "
                             "WHERE notebook_id=%s AND source_id=%s FOR UPDATE" +
                             ("" if wait else " NOWAIT"), (notebook_id, source_id)).fetchone()
            if row is None:
                db.execute("ROLLBACK TO SAVEPOINT governance_receipt_reserve")
                db.execute("RELEASE SAVEPOINT governance_receipt_reserve")
                return False
            if row["revision"] == 0:
                created.append((notebook_id, source_id))
        db.execute("RELEASE SAVEPOINT governance_receipt_reserve")
        reserved.extend(created)
        return True
    except LockNotAvailable:
        db.execute("ROLLBACK TO SAVEPOINT governance_receipt_reserve")
        db.execute("RELEASE SAVEPOINT governance_receipt_reserve")
        if wait:
            raise
        return False


@receipt_lock_scope
def reserve_for_sources(db, notebook_id, source_ids, *, staged=False):
    """Reserve the entire replacement partition set before its first receipt.

    Candidate objects and adjacency stay in SQL. Only distinct owner partitions
    reach Python, so source size does not turn this into an object hydration pass.
    Staged replacement also checks uncertified evidence, matching its delete path.
    """
    ids = sorted(set(source_ids))
    if not ids:
        return
    bound = bind_ids(ids)
    state = db.execute("SELECT source_index_backfilled FROM unified_kg_state WHERE notebook_id=%s",
                       (notebook_id,)).fetchone()
    certified = bool(state and state["source_index_backfilled"])
    doomed = (
        "SELECT id FROM knowledge_objects WHERE notebook_id=%s "
        f"AND {member_of('source_id', bound)} "
        "UNION SELECT object_id AS id FROM knowledge_object_sources "
        f"WHERE notebook_id=%s AND {member_of('source_id', bound)}"
    )
    params = [notebook_id, bound.param, notebook_id, bound.param]
    if staged or not certified:
        # A staged snapshot can cover the entire library. One array binding
        # keeps parameter count fixed while each source drives a containment probe.
        doomed += (f" UNION SELECT ko.id FROM unnest({bound.sql}) changed(source_id) "
                   "JOIN knowledge_objects ko ON ko.notebook_id=%s "
                   "AND ko.evidence @> jsonb_build_array("
                   "jsonb_build_object('source_id',changed.source_id))")
        params.extend((bound.param, notebook_id))
    query = (
        "WITH doomed AS MATERIALIZED (" + doomed + ") "
        "SELECT COALESCE(o.source_id,'') AS source_id FROM doomed d "
        "JOIN knowledge_objects o ON o.id=d.id WHERE o.notebook_id=%s"
    )
    params.append(notebook_id)
    for endpoint, other in (("source_object_id", "target_object_id"),
                            ("target_object_id", "source_object_id")):
        query += (" UNION SELECT COALESCE(o.source_id,'') AS source_id "
                  f"FROM doomed d JOIN knowledge_relations r ON r.{endpoint}=d.id "
                  f"JOIN knowledge_objects o ON o.id=r.{other} AND o.notebook_id=r.notebook_id "
                  "WHERE r.notebook_id=%s")
        params.append(notebook_id)
    for endpoint in ("source_object_id", "target_object_id"):
        query += (" UNION SELECT COALESCE(o.source_id,'') AS source_id "
                  "FROM knowledge_relations r JOIN knowledge_objects o "
                  f"ON o.id=r.{endpoint} AND o.notebook_id=r.notebook_id "
                  f"WHERE r.notebook_id=%s AND {member_of('r.source_id', bound)}")
        params.extend((notebook_id, bound.param))
    affected = {row["source_id"] for row in execute_ids(db, query, tuple(params)).fetchall()}
    _reserve_source_ids(db, notebook_id, affected | set(ids))


def enqueue_for_source_relations(db, notebook_id: str, source_ids, algorithm_version: int) -> None:
    """Requeue endpoint owners before deleting relations by provenance source.

    Endpoint objects can both survive source replacement. Keep the source-bound
    union in SQL so a high-degree source never hydrates all endpoints in Python.
    """
    ids = list(source_ids)
    if not ids:
        return
    bound = bind_ids(ids)
    legs = [
        "SELECT COALESCE(o.source_id,'') AS source_id FROM knowledge_relations r "
        f"JOIN knowledge_objects o ON o.id=r.{endpoint} AND o.notebook_id=r.notebook_id "
        f"WHERE r.notebook_id=%s AND {member_of('r.source_id', bound)}"
        for endpoint in ("source_object_id", "target_object_id")
    ]
    statement = (
        "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version) "
        "SELECT %s,affected.source_id,%s FROM (" + " UNION ".join(legs) + ") affected "
        "WHERE TRUE ORDER BY affected.source_id COLLATE \"C\" "
        "ON CONFLICT (notebook_id,source_id) DO UPDATE SET "
        "revision=kg_relink_work.revision+1,algorithm_version=excluded.algorithm_version,"
        "status='pending',isolated_before=0,isolated_after=0,edges_added=0"
    )
    execute_ids(db, statement, (
        notebook_id, algorithm_version, notebook_id, bound.param, notebook_id, bound.param,
    ))


def enqueue_for_objects(
    db, notebook_id: str, object_ids, algorithm_version: int, *, adjacent: bool = False,
) -> None:
    """Invalidate owners before an object mutation; deletion also affects neighbours.

    The caller supplies a bounded object batch. Endpoint-specific indexed legs
    avoid a notebook-wide relation scan. Callers that lock objects must reserve
    their affected receipts first; receipt-before-object is the publication order.
    """
    ids = list(object_ids)
    if not ids:
        return
    bound = bind_ids(ids)
    sql = (
        "SELECT COALESCE(o.source_id,'') AS source_id FROM knowledge_objects o "
        f"WHERE o.notebook_id=%s AND {member_of('o.id', bound)}"
    )
    params = [notebook_id, bound.param]
    if adjacent:
        for endpoint, other in (("source_object_id", "target_object_id"),
                                ("target_object_id", "source_object_id")):
            sql += (
                " UNION SELECT COALESCE(o.source_id,'') AS source_id "
                "FROM knowledge_relations r JOIN knowledge_objects o "
                f"ON o.id=r.{other} AND o.notebook_id=r.notebook_id "
                f"WHERE r.notebook_id=%s AND {member_of('r.' + endpoint, bound)}"
            )
            params.extend((notebook_id, bound.param))
    rows = execute_ids(db,sql, tuple(params)).fetchall()
    for source_id in sorted({str(row["source_id"]) for row in rows}):
        enqueue(db, notebook_id, source_id, algorithm_version)


def enqueue(db, notebook_id: str, source_id: str, algorithm_version: int) -> None:
    """A graph publication invalidates only its source, including a pending run."""
    db.execute(
        "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version) "
        "VALUES (%s,%s,%s) ON CONFLICT (notebook_id,source_id) DO UPDATE SET "
        "revision=kg_relink_work.revision+1,algorithm_version=excluded.algorithm_version,"
        "status='pending',isolated_before=0,isolated_after=0,edges_added=0",
        (notebook_id, source_id, algorithm_version),
    )


def prepare(db, notebook_id: str, algorithm_version: int, full: bool = False) -> dict:
    """Seed legacy/full work once; a failed full pass resumes its durable receipts."""
    # Parent before state/receipts, matching staged notebook publication.
    db.execute("SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE", (notebook_id,)).fetchone()
    db.execute(
        "INSERT INTO kg_relink_state (notebook_id,algorithm_version) VALUES (%s,0) "
        "ON CONFLICT (notebook_id) DO NOTHING", (notebook_id,),
    )
    state = db.execute(
        "SELECT * FROM kg_relink_state WHERE notebook_id=%s FOR UPDATE", (notebook_id,),
    ).fetchone()
    resumed = bool(state["full_pending"]) and state["algorithm_version"] == algorithm_version
    reset = state["algorithm_version"] != algorithm_version or (full and not resumed)
    if reset:
        seq_row = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        seq = int(seq_row["kg_mutation_seq"]) if seq_row else 0
        # This scan occurs only at first use, algorithm upgrade or explicit full maintenance.
        # It covers empty and deleted-source partitions without materialising all objects.
        # One sorted upsert reserves existing and new keys in the same order as
        # graph writers. A separate bulk UPDATE would lock old rows in heap/plan
        # order and could deadlock with a source replacement holding an earlier key.
        db.execute(
            "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version) "
            "SELECT %s,part.source_id,%s FROM ("
            "SELECT source_id FROM kg_relink_work WHERE notebook_id=%s "
            "UNION SELECT id AS source_id FROM sources WHERE notebook_id=%s "
            "UNION SELECT DISTINCT COALESCE(ko.source_id,'') AS source_id "
            "FROM knowledge_objects ko WHERE ko.notebook_id=%s AND NOT EXISTS ("
            "SELECT 1 FROM sources s WHERE s.notebook_id=ko.notebook_id AND s.id=ko.source_id)"
            ') part WHERE TRUE ORDER BY part.source_id COLLATE "C" '
            "ON CONFLICT (notebook_id,source_id) DO UPDATE SET "
            "revision=kg_relink_work.revision+1,algorithm_version=excluded.algorithm_version,"
            "status='pending',isolated_before=0,isolated_after=0,edges_added=0",
            (notebook_id, algorithm_version, notebook_id, notebook_id, notebook_id),
        )
        db.execute(
            "UPDATE kg_relink_state SET algorithm_version=%s,full_pending=1,"
            "started_at_seq=%s WHERE notebook_id=%s",
            (algorithm_version, seq, notebook_id),
        )
    else:
        seq = int(state["started_at_seq"])
    return {"full_pending": bool(reset or resumed), "resumed": resumed, "started_at_seq": seq}


def page(db, notebook_id: str, after_source_id: str | None, limit: int) -> list:
    """A bounded keyset page; None includes the empty-source partition."""
    return db.execute(
        'SELECT source_id,revision FROM kg_relink_work WHERE notebook_id=%s '
        "AND status='pending' AND (%s::text IS NULL OR source_id COLLATE \"C\" > %s) "
        "ORDER BY source_id COLLATE \"C\" LIMIT %s",
        (notebook_id, after_source_id, after_source_id, max(1, limit)),
    ).fetchall()


def count_pending(db, notebook_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM kg_relink_work WHERE notebook_id=%s AND status='pending'",
        (notebook_id,),
    ).fetchone()["n"])


def current_for_write(db, notebook_id: str, source_id: str, revision: int) -> bool:
    """Called inside the same write transaction as the new edges and receipt."""
    # Relation insertion checks both parent FKs. Take those locks BEFORE the
    # receipt: ingestion already holds its source FOR UPDATE before enqueueing,
    # and staged publication holds notebook/source rows before receipts.
    db.execute("SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE", (notebook_id,)).fetchone()
    db.execute(
        "SELECT id FROM sources WHERE notebook_id=%s AND id=%s FOR KEY SHARE",
        (notebook_id, source_id),
    ).fetchone()
    return db.execute(
        "SELECT revision FROM kg_relink_work WHERE notebook_id=%s AND source_id=%s "
        "AND revision=%s AND status='pending' FOR UPDATE",
        (notebook_id, source_id, revision),
    ).fetchone() is not None


def finish(
    db, notebook_id: str, source_id: str, revision: int,
    before: int, after: int, edges: int,
) -> bool:
    return db.execute(
        "UPDATE kg_relink_work SET status='done',isolated_before=%s,isolated_after=%s,"
        "edges_added=%s WHERE notebook_id=%s AND source_id=%s AND revision=%s AND status='pending'",
        (before, after, edges, notebook_id, source_id, revision),
    ).rowcount == 1


def finish_full(db, notebook_id: str) -> bool:
    """Only a fully drained full pass can close the full-library checkpoint."""
    return db.execute(
        "UPDATE kg_relink_state SET full_pending=0 WHERE notebook_id=%s AND full_pending=1 "
        "AND NOT EXISTS (SELECT 1 FROM kg_relink_work WHERE notebook_id=%s AND status='pending')",
        (notebook_id, notebook_id),
    ).rowcount == 1


def summary(db, notebook_id: str) -> dict:
    row = db.execute(
        "SELECT COUNT(*) AS total,COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0) AS done,"
        "COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS pending,"
        "COALESCE(SUM(isolated_before),0) AS isolated_before,"
        "COALESCE(SUM(isolated_after),0) AS isolated_after,"
        "COALESCE(SUM(edges_added),0) AS edges_added "
        "FROM kg_relink_work WHERE notebook_id=%s", (notebook_id,),
    ).fetchone()
    return {key: int(row[key]) for key in (
        "total", "done", "pending", "isolated_before", "isolated_after", "edges_added",
    )}
