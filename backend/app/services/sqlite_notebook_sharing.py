from __future__ import annotations

from datetime import datetime

from app.domain.repository import remap_json_ids


def _now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _repository_new_id(prefix: str) -> str:
    # Keep one ID policy and preserve sqlite_repository._new_id monkeypatches.
    # Task 9: production code now reaches this policy through
    # RepositoryCompatibilitySeams.new_id; kept as a documented compatibility
    # helper for external callers of this module.
    from app.services.sqlite_repository import _new_id

    return _new_id(prefix)


def _copy_chunk_size() -> int:
    # _COPY_CHUNK has long been a test/perf tuning seam on sqlite_repository.
    # Task 9: production code now reads it through
    # RepositoryCompatibilitySeams.copy_chunk_size (same late binding); kept
    # as a documented compatibility helper for external callers.
    from app.services.sqlite_repository import _COPY_CHUNK

    return int(_COPY_CHUNK)


def _remap_json_ids(value, maps: dict):
    """Compatibility spelling for the domain-owned pure remapping value."""
    return remap_json_ids(value, maps)


class SQLiteNotebookSharingMixin:
    """[compatibility export] Former sharing/deep-copy/membership mixin.

    Task 9 recomposed this domain: row-level SQL lives in
    app.repositories.sqlite.sharing_store.SharingStore, orchestration in
    app.services.notebook_sharing (NotebookSharingService +
    NotebookCopyService), and the public SQLiteRepository facade keeps
    frozen-signature delegates.  The facade no longer inherits this class;
    it is kept importable so legacy imports don't break.
    """
