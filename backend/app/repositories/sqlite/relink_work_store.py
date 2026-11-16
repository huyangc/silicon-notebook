"""Durable source-partition receipts for relink, owned by the caller's transaction."""
from __future__ import annotations

from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.id_binding import bind_ids, member_of


def enqueue_for_objects(
    db, notebook_id: str, object_ids, algorithm_version: int, *, adjacent: bool = False,
) -> None:
    """Invalidate owners before an object mutation; deletion also affects neighbours.

    The caller supplies a bounded object batch. Endpoint-specific indexed legs
    avoid a notebook-wide relation scan. No source lock is taken here: mutation
    owners may already hold object locks, and receipts have no source foreign key.
    """
    ids = list(object_ids)
    if not ids:
        return
    bound = bind_ids(ids)
    sql = (
        "SELECT COALESCE(o.source_id,'') AS source_id FROM knowledge_objects o "
        f"WHERE o.notebook_id=? AND {member_of('o.id', bound)}"
    )
    params = [notebook_id, bound.param]
    if adjacent:
        for endpoint, other in (("source_object_id", "target_object_id"),
                                ("target_object_id", "source_object_id")):
            sql += (
                " UNION SELECT COALESCE(o.source_id,'') AS source_id "
                "FROM knowledge_relations r JOIN knowledge_objects o "
                f"ON o.id=r.{other} AND o.notebook_id=r.notebook_id "
                f"WHERE r.notebook_id=? AND {member_of('r.' + endpoint, bound)}"
            )
            params.extend((notebook_id, bound.param))
    rows = db.execute(sql, tuple(params)).fetchall()
    for source_id in sorted({str(row["source_id"]) for row in rows}):
        enqueue(db, notebook_id, source_id, algorithm_version)

def enqueue(db, notebook_id: str, source_id: str, algorithm_version: int) -> None:
    """A graph publication invalidates only its source, including a pending run."""
    db.execute(
        "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version) "
        "VALUES (?,?,?) ON CONFLICT (notebook_id,source_id) DO UPDATE SET "
        "revision=kg_relink_work.revision+1,algorithm_version=excluded.algorithm_version,"
        "status='pending',isolated_before=0,isolated_after=0,edges_added=0",
        (notebook_id, source_id, algorithm_version),
    )


def prepare(db, notebook_id: str, algorithm_version: int, full: bool = False) -> dict:
    """Seed legacy/full work once; a failed full pass resumes its durable receipts."""
    db.execute(
        "INSERT INTO kg_relink_state (notebook_id,algorithm_version) VALUES (?,0) "
        "ON CONFLICT (notebook_id) DO NOTHING", (notebook_id,),
    )
    state = db.execute(
        "SELECT * FROM kg_relink_state WHERE notebook_id=?", (notebook_id,),
    ).fetchone()
    resumed = bool(state["full_pending"]) and state["algorithm_version"] == algorithm_version
    reset = state["algorithm_version"] != algorithm_version or (full and not resumed)
    if reset:
        seq_row = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        seq = int(seq_row["kg_mutation_seq"]) if seq_row else 0
        db.execute(
            "UPDATE kg_relink_work SET revision=revision+1,algorithm_version=?,"
            "status='pending',isolated_before=0,isolated_after=0,edges_added=0 "
            "WHERE notebook_id=?", (algorithm_version, notebook_id),
        )
        # This scan occurs only at first use, algorithm upgrade or explicit full maintenance.
        # It covers empty and deleted-source partitions without materialising all objects.
        db.execute(
            "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version) "
            "SELECT ?,part.source_id,? FROM ("
            "SELECT id AS source_id FROM sources WHERE notebook_id=? "
            "UNION SELECT DISTINCT COALESCE(ko.source_id,'') AS source_id "
            "FROM knowledge_objects ko WHERE ko.notebook_id=? AND NOT EXISTS ("
            "SELECT 1 FROM sources s WHERE s.notebook_id=ko.notebook_id AND s.id=ko.source_id)"
            ") part WHERE TRUE ON CONFLICT (notebook_id,source_id) DO NOTHING",
            (notebook_id, algorithm_version, notebook_id, notebook_id),
        )
        db.execute(
            "UPDATE kg_relink_state SET algorithm_version=?,full_pending=1,"
            "started_at_seq=? WHERE notebook_id=?",
            (algorithm_version, seq, notebook_id),
        )
    else:
        seq = int(state["started_at_seq"])
    return {"full_pending": bool(reset or resumed), "resumed": resumed, "started_at_seq": seq}


def page(db, notebook_id: str, after_source_id: str | None, limit: int) -> list:
    """A bounded keyset page; None includes the empty-source partition."""
    return db.execute(
        'SELECT source_id,revision FROM kg_relink_work WHERE notebook_id=? '
        "AND status='pending' AND (? IS NULL OR source_id COLLATE BINARY > ?) "
        "ORDER BY source_id COLLATE BINARY LIMIT ?",
        (notebook_id, after_source_id, after_source_id, max(1, limit)),
    ).fetchall()


def count_pending(db, notebook_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM kg_relink_work WHERE notebook_id=? AND status='pending'",
        (notebook_id,),
    ).fetchone()["n"])


def current_for_write(db, notebook_id: str, source_id: str, revision: int) -> bool:
    """Called inside the same write transaction as the new edges and receipt."""
    if not db.in_transaction:
        SqliteDatabase.begin_guarded_write(db)
    return db.execute(
        "SELECT revision FROM kg_relink_work WHERE notebook_id=? AND source_id=? "
        "AND revision=? AND status='pending'",
        (notebook_id, source_id, revision),
    ).fetchone() is not None


def finish(
    db, notebook_id: str, source_id: str, revision: int,
    before: int, after: int, edges: int,
) -> bool:
    return db.execute(
        "UPDATE kg_relink_work SET status='done',isolated_before=?,isolated_after=?,"
        "edges_added=? WHERE notebook_id=? AND source_id=? AND revision=? AND status='pending'",
        (before, after, edges, notebook_id, source_id, revision),
    ).rowcount == 1


def finish_full(db, notebook_id: str) -> bool:
    """Only a fully drained full pass can close the full-library checkpoint."""
    return db.execute(
        "UPDATE kg_relink_state SET full_pending=0 WHERE notebook_id=? AND full_pending=1 "
        "AND NOT EXISTS (SELECT 1 FROM kg_relink_work WHERE notebook_id=? AND status='pending')",
        (notebook_id, notebook_id),
    ).rowcount == 1


def summary(db, notebook_id: str) -> dict:
    row = db.execute(
        "SELECT COUNT(*) AS total,COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0) AS done,"
        "COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) AS pending,"
        "COALESCE(SUM(isolated_before),0) AS isolated_before,"
        "COALESCE(SUM(isolated_after),0) AS isolated_after,"
        "COALESCE(SUM(edges_added),0) AS edges_added "
        "FROM kg_relink_work WHERE notebook_id=?", (notebook_id,),
    ).fetchone()
    return {key: int(row[key]) for key in (
        "total", "done", "pending", "isolated_before", "isolated_after", "edges_added",
    )}
