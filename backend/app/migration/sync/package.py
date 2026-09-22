"""Sync package layout constants and the portable row encoding
(docs/incremental-sync-design.md §8).

A sync package is a directory; this module owns the names inside it and the
value encoding its ``*.jsonl`` files use, so the exporter
(``app.migration.sync.export``) and the importer read and write exactly one
format rather than two that drift apart.

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

import base64
import json
import re
from datetime import datetime, timezone
from typing import Any

# Bumped only when the layout or encoding below changes incompatibly; import
# preflight rejects a package whose format_version it does not know.
PACKAGE_FORMAT_VERSION = 1

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

# Sole key of the object a byte string is encoded as.
BYTES_KEY = "$bytes"


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


def utc_timestamp_text(value: Any) -> Any:
    """One TIMESTAMP COLUMN's portable value (see the module docstring).

    Callers must only apply this to a column the schema says is a timestamp;
    it makes no attempt to decide that for itself.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat()


def json_line(payload: Any) -> str:
    """One ``*.jsonl`` line. ``ensure_ascii=False`` keeps CJK content readable
    (and the package smaller); ``sort_keys=True`` makes the bytes -- and
    therefore ``checksums.json`` -- depend on the row's content alone, not on
    the column order a particular driver handed back."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def json_document(payload: Any) -> str:
    """``manifest.json``/``checksums.json`` text, same determinism rules."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def encode_value(value: Any) -> Any:
    """Encode one already-SQLite-shaped column value for JSON.

    ``bool`` is checked before the byte types and folded to 0/1 because that
    is the SQLite shape (§8) -- and because a PostgreSQL ``boolean`` column
    hands psycopg a real ``bool``, which would otherwise travel as JSON
    ``true``/``false`` and reach a SQLite target as a different literal than
    every other environment writes.

    This handles only what JSON cannot carry natively. Timestamp and JSON
    columns are the exporter's business, because both need to know WHICH
    column a value came from -- see ``utc_timestamp_text``.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {BYTES_KEY: base64.b64encode(bytes(value)).decode("ascii")}
    return value


def decode_value(value: Any) -> Any:
    """Inverse of ``encode_value``. Only the exact single-key ``$bytes``
    object decodes back to bytes; every other value passes through, so a JSON
    column carried as text is untouched."""
    if isinstance(value, dict) and tuple(value) == (BYTES_KEY,):
        return base64.b64decode(value[BYTES_KEY])
    return value


# A single filesystem path SEGMENT drawn from package content that will be
# spliced into a real path (a notebook id becoming a directory name, mainly).
# Deliberately narrow -- this is not "what a notebook id happens to look
# like today", it is "what is safe to join onto a trusted base path without
# a traversal or a shell-metacharacter surprise". ``import_.py`` is the only
# caller today; kept here (not there) because the SAME rule has to hold for
# whatever the exporter writes into ``manifest.notebooks``/``checksums.json``
# and whatever the importer reads back out of them -- one rule, not two that
# can drift.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_identifier(value: str) -> bool:
    """True when ``value`` is safe to use as ONE filesystem path segment.

    Non-empty, drawn only from ``[A-Za-z0-9._-]``, must not start with ``.``
    (which rules out ``.``, ``..``, and any dotfile in one stroke), and must
    not contain ``..`` anywhere (redundant with the leading-dot rule for the
    exact traversal token, kept as a second, independent check rather than
    relying on the regex alone to carry the whole rule)."""
    if not value or value.startswith(".") or ".." in value:
        return False
    return bool(_SAFE_IDENTIFIER_RE.match(value))


def is_safe_path_segment(value: str) -> bool:
    """True when ``value`` can be ONE segment of a package-relative path.

    Looser than ``is_safe_identifier`` on purpose: the last segment of a
    ``files/**`` path is an uploaded file's name, which ``stored_upload_name``
    keeps as the user typed it -- ``研究.pdf``, ``meeting notes.pdf`` -- so a
    character whitelist here would reject packages the exporter itself
    produced (codex #772 r10). What a segment must NOT be is what lets it
    leave its directory: empty, ``.``/``..``, or carrying a separator or NUL.
    Containment is proven separately by resolving the path against its root."""
    if not value or value in (".", ".."):
        return False
    return "/" not in value and "\\" not in value and "\x00" not in value


def is_safe_relative_path(value: str) -> bool:
    """True when ``value`` is a package-relative, ``/``-separated path (the
    shape ``checksums.json``'s keys and ``rows_path()``/``notebook_files_dir()``/
    ``notebook_assets_dir()`` take) whose every segment is
    ``is_safe_path_segment``. Refuses an empty string, a leading ``/`` or
    ``\\`` (absolute on POSIX or Windows), and a Windows drive prefix
    (``C:...``) -- and, through the per-segment check, any ``..`` hop.
    Notebook and package ids are held to the stricter ``is_safe_identifier``
    by the importer's preflight; this function only guards traversal."""
    if not value or value.startswith("/") or value.startswith("\\"):
        return False
    if len(value) >= 2 and value[1] == ":":
        return False
    return all(is_safe_path_segment(segment) for segment in value.split("/"))


def encode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {name: encode_value(value) for name, value in row.items()}


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {name: decode_value(value) for name, value in row.items()}
