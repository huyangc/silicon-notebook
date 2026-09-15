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
"""
from __future__ import annotations

from app.repositories.postgres.access_sql import (
    MEMBER_PROBE_FOR_SHARE_SQL,
    READ_GRANT_DIRECT_FOR_SHARE_SQL,
    READ_GRANT_GROUP_CHAIN_FOR_SHARE_SQL,
    read_grant_direct_params,
    read_grant_group_chain_params,
)


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


__all__ = ["lock_reader_access_on"]
