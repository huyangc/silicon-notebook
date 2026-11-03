"""Row-locked live read-authority chain for guarded admin-detail projections.

The SQL shapes stay in ``access_sql`` (the single definition point for
authorization predicates). This module owns the one execution order that
locks a complete, currently-valid self-service read chain so it cannot be
revoked while a detail response that carries notebook content is still being
assembled. Keeping the order here, rather than as a private helper of one
store, is what lets several stores share it without a second copy:

* ``ask_state_store.AskStateStore.guarded_ask_detail`` (answer + trace);
* ``report_store.ReportStore.guarded_report_detail`` (report body + references).

Callers must already hold the notebook root lease (``FOR KEY SHARE`` on the
``notebooks`` row) and pass that row's ``created_by`` as
``notebook_owner_id``; the owner arm is answered from that locked row.

``lock_live_reader_on`` is the same order with the root taken here (``FOR
SHARE`` and the live predicate): the auto user-memory writes use it so a
concurrent access loss (``delete_grant``, a member or group removal, a
notebook leaving the live states) either commits before the write and is
seen, or waits for it and then deletes the row it wrote (plan B3/B9):

* ``memory_suggest_store.MemorySuggestStore.note_signal`` (the per-ask
  counter upsert);
* the suggestion write transaction's scope lock in ``memory_store`` is the
  same three steps and is meant to call this helper too (one definition).
"""
from __future__ import annotations

from collections.abc import Iterable

from app.repositories.postgres.access_sql import (
    MEMBER_PROBE_FOR_SHARE_SQL,
    NOTEBOOK_LIVE_SQL,
    READ_GRANT_DIRECT_FOR_SHARE_SQL,
    READ_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
    read_grant_direct_params,
    read_grant_group_chain_params,
)
from app.repositories.postgres.id_binding import bind_ids, execute_ids, member_of


def lock_read_changes_on(db, notebook_ids: Iterable[str]) -> None:
    """Serialize read-path changes BEFORE changing any authority edge.

    Group writers first hold their existing group root; all paths then lock
    affected notebooks in bytewise id order, then member/grant rows, then
    ordered job rows. No notebook holder subsequently takes a group root.
    FOR UPDATE also conflicts with the live reader's root lease, so new
    signals/writes cannot cross removal and its same-transaction cleanup.
    Group membership gains/promotions take the same roots before exposing a
    new read arm; otherwise an uncommitted revoke can persist a false loss.
    """
    ids = sorted({str(value) for value in notebook_ids if value})
    if not ids:
        return
    bound = bind_ids(ids)
    execute_ids(
        db, f"SELECT id FROM notebooks WHERE {member_of('id', bound)} "
        'ORDER BY id COLLATE "C" FOR UPDATE', (bound.param,),
    ).fetchall()


def lock_reader_access_on(
    db,
    notebook_id: str,
    reader_id: str,
    *,
    notebook_owner_id: str,
) -> bool:
    """Lock one complete live read-authority chain, root already leased."""
    if notebook_owner_id == reader_id:
        return True
    if db.execute(
        MEMBER_PROBE_FOR_SHARE_SQL, (notebook_id, reader_id)
    ).fetchone() is not None:
        return True
    if db.execute(
        READ_GRANT_DIRECT_FOR_SHARE_SQL,
        read_grant_direct_params(notebook_id, reader_id),
    ).fetchone() is not None:
        return True
    return db.execute(
        READ_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
        read_grant_group_chain_params(notebook_id, reader_id),
    ).fetchone() is not None


def lock_live_reader_on(db, notebook_id: str, reader_id: str) -> bool | None:
    """Lock the live notebook root (``FOR SHARE``), then one complete live
    read-authority chain for ``reader_id``, inside the caller's transaction.

    Three states, so a caller can tell why it stopped: ``None`` = no such
    notebook or it is not live (copying/deleting/importing/pending_delete);
    ``False`` = live but ``reader_id`` cannot read it; ``True`` = readable,
    and the rows that make it so stay share-locked until the caller commits.
    """
    root = db.execute(
        f"SELECT created_by FROM notebooks WHERE id=%s AND {NOTEBOOK_LIVE_SQL} "
        "FOR SHARE",
        (notebook_id,),
    ).fetchone()
    if root is None:
        return None
    return lock_reader_access_on(
        db, notebook_id, reader_id, notebook_owner_id=root["created_by"]
    )


__all__ = ["lock_live_reader_on", "lock_reader_access_on", "lock_read_changes_on"]
