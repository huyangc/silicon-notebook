"""Sync package layout constants and the portable row encoding
(docs/incremental-sync-design.md §8).

A sync package is a directory; this module owns the names inside it and the
value encoding its ``*.jsonl`` files use, so the exporter
(``app.migration.sync.export``) and the importer read and write exactly one
format rather than two that drift apart. The encoding itself is defined in
``app.domain.portable_rows`` (the single-file notebook archive writes the same
row shape) and re-exported below; what follows describes it.

The row encoding is the **SQLite shape**: a row is a JSON object keyed by
column name whose values are text/integer/null as SQLite would hold them
(booleans as 0/1, JSON columns as text, timestamps as ISO 8601 strings). A
SQLite target lands such a row verbatim; a PostgreSQL target reuses the
existing ``app.migration.shadow.transform`` SQLite->PG conversion. The one
value JSON cannot carry natively is a byte string, so a ``bytea``/``BLOB``
column becomes the single-key object ``{"$bytes": "<base64>"}`` -- an object
rather than a bare base64 string, so a text column that happens to contain
base64 is never mistaken for binary on the way back in.

**Timestamp columns are normalized to UTC** (``...+00:00``), so the package
bytes -- and therefore ``checksums.json`` -- do not depend on which host ran
the export. This is applied BY COLUMN, never by looking at a value: which
columns are timestamps is decided by the exporter from the migration DDL
contract, so an ordinary text column holding something that looks like a
timestamp is never rewritten. Three rules inside such a column:

- ``None`` stays ``None``. A missing instant is null, not the empty string.
- An aware value (a ``datetime`` from PostgreSQL, or SQLite text carrying an
  offset) is re-expressed at ``+00:00`` -- the same instant, one spelling.
- A value with NO zone information is left exactly as it is. This module will
  not guess a zone for it, because guessing would silently move the instant by
  the exporting host's offset. The empty string SQLite uses as its "no time"
  sentinel falls under this rule and travels unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The row encoding, the deterministic JSON writers and the path-safety rules
# moved to ``app.domain.portable_rows`` so the single-file notebook archive
# writes exactly the same row shape. Re-exported here under their historical
# names: one definition, two formats.
from app.domain.portable_rows import (
    _SAFE_IDENTIFIER_RE,
    BYTES_KEY,
    column_encoder,
    decode_row,
    decode_value,
    encode_row,
    encode_value,
    is_safe_identifier,
    is_safe_path_segment,
    is_safe_relative_path,
    json_document,
    json_line,
    utc_timestamp_text,
)

__all__ = [
    "ASSET_FILES_DIR",
    "BYTES_KEY",
    "CHECKSUMS_NAME",
    "DELETES_NAME",
    "FILES_DIR",
    "KG_EPOCHS_NAME",
    "MANIFEST_NAME",
    "MODE_FULL",
    "MODE_INCREMENTAL",
    "NOTEBOOK_FILES_DIR",
    "PACKAGE_FORMAT_VERSION",
    "READABLE_FORMAT_VERSIONS",
    "ROWS_DIR",
    "USERS_NAME",
    "_SAFE_IDENTIFIER_RE",
    "column_encoder",
    "decode_row",
    "decode_value",
    "delete_entry",
    "encode_row",
    "encode_value",
    "is_safe_identifier",
    "is_safe_path_segment",
    "is_safe_relative_path",
    "json_document",
    "json_line",
    "notebook_assets_dir",
    "notebook_files_dir",
    "package_dir_name",
    "rows_path",
    "utc_timestamp_text",
]

# Bumped only when the layout or encoding below changes incompatibly; import
# preflight rejects a package whose format_version it does not know.
#
# 2 (PR-3b): ``manifest.json`` gains ``mode``, ``base_package_id``,
# ``deleted_notebooks``, ``deletes`` and ``kg_epochs``, and ``from_seq``/
# ``to_seq`` stop being hard-wired zeros -- a v2 ``mode=full`` package now
# carries the change-log watermark this export saw. The ROW ENCODING and the
# directory layout are byte-for-byte what v1 had, so a v2 full package is a v1
# full package plus manifest fields; what a v1 reader could not do is tell a
# full package from an incremental one, which is exactly why the version moved.
#
# 3 (design doc §11 rulings, 2026-10-19): the same layout, but the package
# means something a v2 reader would misapply rather than refuse. The Knowhow
# edit history may be left out (``manifest.knowhow_history``, required from
# v3 on), ``group_members`` always travels empty and ``groups`` only as
# ``(id, name)``, because the importer maps groups by name and never creates
# one. A v2 importer reading that would quietly insert a nameless,
# memberless group under its source id beside a grant on it -- so the version
# moves and a v2 importer refuses the package outright. This build reads v2
# packages too (``READABLE_FORMAT_VERSIONS``): absent ``knowhow_history``
# means the history is there, as it always was in v2.
PACKAGE_FORMAT_VERSION = 3
READABLE_FORMAT_VERSIONS = (2, 3)

# ``manifest.mode``. A full package reconciles the notebooks it names (the
# importer deletes target rows the package does not carry); an incremental one
# carries only the window's final states plus ``deletes.jsonl`` and must never
# be replayed as a reconciliation. ``import_._classify_package`` is the one
# place that decides which of the two a package is, and every import phase
# branches on its answer.
MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"

MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.json"
USERS_NAME = "users.jsonl"
DELETES_NAME = "deletes.jsonl"
KG_EPOCHS_NAME = "kg_epochs.jsonl"
ROWS_DIR = "rows"
FILES_DIR = "files"
# The two on-disk roots a notebook owns under ``storage/``: its uploaded
# source files and its attachment bodies (notebook_assets' metadata rows
# travel with the notebook, their bytes live here). Both are mirrored into
# the package under the same names.
NOTEBOOK_FILES_DIR = "notebooks"
ASSET_FILES_DIR = "assets"


def package_dir_name(
    source_env: str, from_seq: int, to_seq: int, package_id: str
) -> str:
    """``sync-<source_env>-<from_seq>-<to_seq>-<package_id[:8]>``.

    The id suffix is what keeps two packages covering the same sequence range
    (a full export re-run, which is always ``0-0``) from colliding in one
    output directory.
    """
    return f"sync-{source_env}-{from_seq}-{to_seq}-{package_id[:8]}"


def rows_path(table: str) -> str:
    """This table's package-relative ``rows/<table>.jsonl`` path."""
    return f"{ROWS_DIR}/{table}.jsonl"


def notebook_files_dir(notebook_id: str) -> str:
    """This notebook's package-relative source-file directory."""
    return f"{FILES_DIR}/{NOTEBOOK_FILES_DIR}/{notebook_id}"


def notebook_assets_dir(notebook_id: str) -> str:
    """This notebook's package-relative attachment-body directory."""
    return f"{FILES_DIR}/{ASSET_FILES_DIR}/{notebook_id}"


def delete_entry(
    table: str,
    key: Mapping[str, Any],
    notebook_id: str | None,
    parent_key: str | None,
) -> dict[str, Any]:
    """One ``deletes.jsonl`` line: the row identity the source dropped inside
    this package's window, plus what the exporter could say about where it
    belonged (docs/incremental-sync-design.md §8).

    ``key`` is the row's synchronization key as a column -> value mapping, the
    same shape ``sync_change_log.key_json`` carries -- NOT a positional tuple,
    because the two backends spell that JSON object's key order differently
    (SQLite ``json_object`` keeps the declaration order, PostgreSQL ``jsonb``
    normalizes it) and a reader must be able to match by column name rather
    than by position.

    ``notebook_id`` is deliberately nullable: a PARENT-scoped row whose parent
    chain was deleted in the same window has no notebook left to resolve to,
    and the exporter records that honestly instead of guessing (§7 "归属").
    The importer's delete replay uses it as the last piece of evidence for an
    orphan -- a row whose chain it cannot walk at the target is deleted only
    when this field names a notebook in scope, and is otherwise a no-op (§11).

    ``parent_key`` carries the change log's own ``parent_key``. It has **no
    importer-side consumer**: the replay resolves attribution from the TARGET
    row's own parent pointer, which is the only thing that can say who the row
    about to be deleted belongs to THERE -- a key the source recorded
    describes the source's chain, not this one's. Kept because it is the
    honest record of what the log carried, and because an operator reading a
    package has nothing else to go on.
    """
    return {
        "table": table,
        "key": dict(key),
        "notebook_id": notebook_id or None,
        "parent_key": parent_key or None,
    }
