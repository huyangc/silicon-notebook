from __future__ import annotations

from datetime import datetime


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _repository_new_id(prefix: str) -> str:
    # Keep one ID policy and preserve sqlite_repository._new_id monkeypatches.
    # Task 9: production code now reaches this policy through
    # RepositoryCompatibilitySeams.new_id; kept as a documented compatibility
    # helper for external callers of this module.
    from app.services import sqlite_repository

    return sqlite_repository._new_id(prefix)


def _copy_chunk_size() -> int:
    # _COPY_CHUNK has long been a test/perf tuning seam on sqlite_repository.
    # Task 9: production code now reads it through
    # RepositoryCompatibilitySeams.copy_chunk_size (same late binding); kept
    # as a documented compatibility helper for external callers.
    from app.services import sqlite_repository

    return int(sqlite_repository._COPY_CHUNK)


def _remap_json_ids(value, maps: dict):
    """Recursively rewrite copied source/element/object references in JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("element_id", "source_id", "object_id") and isinstance(item, str):
                out[key] = maps.get(key, {}).get(item, item)
            elif key == "element_ids" and isinstance(item, list):
                mapping = maps.get("element_ids", {})
                out[key] = [
                    mapping.get(child, child)
                    if isinstance(child, str)
                    else _remap_json_ids(child, maps)
                    for child in item
                ]
            else:
                out[key] = _remap_json_ids(item, maps)
        return out
    if isinstance(value, list):
        return [_remap_json_ids(item, maps) for item in value]
    return value


class SQLiteNotebookSharingMixin:
    """[compatibility export] Former sharing/deep-copy/membership mixin.

    Task 9 recomposed this domain: row-level SQL lives in
    app.repositories.sqlite.sharing_store.SharingStore, orchestration in
    app.services.notebook_sharing (NotebookSharingService +
    NotebookCopyService), and the public SQLiteRepository facade keeps
    frozen-signature delegates.  The facade no longer inherits this class;
    it is kept importable so legacy imports don't break.
    """
