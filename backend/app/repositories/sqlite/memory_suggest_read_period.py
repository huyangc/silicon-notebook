"""Local last-read-loss fences; caller owns the authorization write transaction."""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from app.repositories.sqlite.access_sql import read_access_clause
from app.repositories.sqlite.id_binding import bind_ids, drive_by


def ask_after_read_loss_sql(ask_alias: str = "a") -> str:
    """Pure SQL for a trusted Ask alias: read-loss is an open time boundary.

    This is not an eligibility filter. Corrected/raw-turn consumers retain
    their original adjacency and check BOTH turns against this period.
    """
    return (
        "NOT EXISTS (SELECT 1 FROM memory_suggest_read_fences read_period_fence "
        f"WHERE read_period_fence.notebook_id={ask_alias}.notebook_id "
        f"AND read_period_fence.owner_id={ask_alias}.created_by "
        f"AND sn_utc_us(read_period_fence.lost_at) >= sn_utc_us({ask_alias}.created_at))"
    )


def readers_before_loss_on(db, notebook_ids: Iterable[str]) -> list[tuple[str, str]]:
    """Enabled readers before mutation, including owners with no job yet."""
    ids = sorted({str(value) for value in notebook_ids if value})
    if not ids:
        return []
    bound = bind_ids(ids)
    readable = read_access_clause("nb", user_ref="u.owner_id")
    rows = db.execute(
        "SELECT nb.id AS notebook_id, u.owner_id FROM notebooks nb "
        "CROSS JOIN memory_suggest_users u WHERE "
        f"{drive_by('nb.id', bound)} AND u.enabled=1 AND {readable}",
        (bound.param,),
    ).fetchall()
    return [(str(row["notebook_id"]), str(row["owner_id"])) for row in rows]


def record_read_loss_on(db, readers: Sequence[tuple[str, str]], *, now: str) -> None:
    """Persist only genuine before-readable -> after-unreadable changes.

    One bounded-by-affected-readers JSON binding, one set-based insert; no
    per-owner transaction or historical content. Existing fences only advance.
    """
    if not readers:
        return
    readable = read_access_clause("nb", user_ref="json_extract(p.value, '$[1]')")
    db.execute(
        "INSERT INTO memory_suggest_read_fences (notebook_id, owner_id, lost_at) "
        "SELECT json_extract(p.value, '$[0]'), json_extract(p.value, '$[1]'), ? "
        "FROM json_each(?) p WHERE EXISTS (SELECT 1 FROM memory_suggest_users u "
        "WHERE u.owner_id=json_extract(p.value, '$[1]') AND u.enabled=1) "
        "AND EXISTS (SELECT 1 FROM notebooks WHERE id=json_extract(p.value, '$[0]')) "
        "AND NOT EXISTS (SELECT 1 FROM notebooks nb "
        f"WHERE nb.id=json_extract(p.value, '$[0]') AND {readable}) "
        "ORDER BY json_extract(p.value, '$[0]'), json_extract(p.value, '$[1]') "
        "ON CONFLICT (notebook_id, owner_id) DO UPDATE SET lost_at=excluded.lost_at "
        "WHERE sn_utc_us(excluded.lost_at) > sn_utc_us(memory_suggest_read_fences.lost_at)",
        (now, json.dumps(list(readers))),
    )

