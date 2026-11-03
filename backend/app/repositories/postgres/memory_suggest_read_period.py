"""Local last-read-loss fences, under the caller's common notebook root locks."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.repositories.postgres.access_sql import read_access_clause
from app.repositories.postgres.id_binding import bind_ids, execute_ids, member_of

_MODE_READ_PERIOD_LOCK_KEY = "silicon-notebook:memory-suggest:mode-read-period:v1"


def lock_mode_read_period_on(db, *, exclusive: bool = False) -> None:
    """First lock of low-frequency mode/read-loss writes only.

    Modes (and account switch deletion) take exclusive; read-loss writers
    share it with each other. No mode read or answer completion takes it.
    It linearizes enabled-reader capture against uncommitted opt-ins.
    """
    sql = ("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))" if exclusive else
           "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))")
    db.execute(sql, (_MODE_READ_PERIOD_LOCK_KEY,))


def ask_after_read_loss_sql(ask_alias: str = "a") -> str:
    """Pure SQL for a trusted Ask alias: read-loss is an open time boundary.

    This is not an eligibility filter. Corrected/raw-turn consumers retain
    their original adjacency and check BOTH turns against this period.
    """
    return (
        "NOT EXISTS (SELECT 1 FROM memory_suggest_read_fences read_period_fence "
        f"WHERE read_period_fence.notebook_id={ask_alias}.notebook_id "
        f"AND read_period_fence.owner_id={ask_alias}.created_by "
        f"AND read_period_fence.lost_at >= {ask_alias}.created_at)"
    )


def readers_before_loss_on(db, notebook_ids: Iterable[str]) -> list[tuple[str, str]]:
    """Capture enabled readers before mutation, without requiring a job row."""
    ids = sorted({str(value) for value in notebook_ids if value})
    if not ids:
        return []
    bound = bind_ids(ids)
    readable = read_access_clause("nb", user_ref="u.owner_id")
    rows = execute_ids(
        db, "SELECT nb.id AS notebook_id, u.owner_id FROM notebooks nb "
        "CROSS JOIN memory_suggest_users u WHERE "
        f"{member_of('nb.id', bound)} AND u.enabled=1 AND {readable}",
        (bound.param,),
    ).fetchall()
    return [(str(row["notebook_id"]), str(row["owner_id"])) for row in rows]


def record_read_loss_on(db, readers: Sequence[tuple[str, str]], *, now) -> None:
    """One set-based write of actual last-read loss; current readers keep epoch.

    Root locks precede this capture/mutation/write chain. Ordered keys prevent
    overlapping fence writes from inventing a second lock order.
    """
    if not readers:
        return
    readable = read_access_clause("nb", user_ref="p.owner_id")
    db.execute(
        "INSERT INTO memory_suggest_read_fences (notebook_id, owner_id, lost_at) "
        "SELECT p.notebook_id, p.owner_id, %s::timestamptz "
        "FROM unnest(%s::text[], %s::text[]) p(notebook_id, owner_id) "
        "WHERE EXISTS (SELECT 1 FROM memory_suggest_users u "
        "WHERE u.owner_id=p.owner_id AND u.enabled=1) "
        "AND EXISTS (SELECT 1 FROM notebooks WHERE id=p.notebook_id) "
        "AND NOT EXISTS (SELECT 1 FROM notebooks nb "
        f"WHERE nb.id=p.notebook_id AND {readable}) "
        'ORDER BY p.notebook_id COLLATE "C", p.owner_id COLLATE "C" '
        "ON CONFLICT (notebook_id, owner_id) DO UPDATE SET lost_at=EXCLUDED.lost_at "
        "WHERE EXCLUDED.lost_at > memory_suggest_read_fences.lost_at",
        (now, [item[0] for item in readers], [item[1] for item in readers]),
    )

