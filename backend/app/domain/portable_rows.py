"""The portable row encoding shared by every cross-environment format.

Two formats carry a notebook's rows out of one deployment and into another: the
sync package (``app.migration.sync``, docs/incremental-sync-design.md §8) and
the single-file notebook archive (``app.domain.notebook_archive``). Both write
rows in exactly one shape, defined here, so the two can never drift apart and a
reader never has to know which backend wrote a file. ``app.migration.sync.package``
re-exports every name below under its historical spelling.

The row encoding is the **SQLite shape**: a row is a JSON object keyed by
column name whose values are text/integer/null as SQLite would hold them
(booleans as 0/1, JSON columns as text, timestamps as ISO 8601 strings). A
SQLite target lands such a row verbatim; a PostgreSQL target reuses the
existing ``app.migration.shadow.transform`` SQLite->PG conversion. The one
value JSON cannot carry natively is a byte string, so a ``bytea``/``BLOB``
column becomes the single-key object ``{"$bytes": "<base64>"}`` -- an object
rather than a bare base64 string, so a text column that happens to contain
base64 is never mistaken for binary on the way back in.

**Timestamp columns are normalized to UTC** (``...+00:00``), so a file's bytes
-- and therefore its checksums -- do not depend on which host ran the export.
This is applied BY COLUMN, never by looking at a value: which columns are
timestamps is decided by the writer from the migration DDL contract, so an
ordinary text column holding something that looks like a timestamp is never
rewritten. Three rules inside such a column:

- ``None`` stays ``None``. A missing instant is null, not the empty string.
- An aware value (a ``datetime`` from PostgreSQL, or SQLite text carrying an
  offset) is re-expressed at ``+00:00`` -- the same instant, one spelling.
- A value with NO zone information is left exactly as it is. This module will
  not guess a zone for it, because guessing would silently move the instant by
  the exporting host's offset. The empty string SQLite uses as its "no time"
  sentinel falls under this rule and travels unchanged.

Pure standard library: the domain layer imports nothing from the application.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from typing import Any

# Sole key of the object a byte string is encoded as.
BYTES_KEY = "$bytes"


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
    (and the file smaller); ``sort_keys=True`` makes the bytes -- and
    therefore the checksums -- depend on the row's content alone, not on
    the column order a particular driver handed back."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def json_document(payload: Any) -> str:
    """A manifest/checksum document's text, same determinism rules."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def json_column_text(value: Any) -> Any:
    """A JSON column's portable form: text, or NULL. psycopg hands back a
    decoded ``list``/``dict`` for ``jsonb``; SQLite already holds text.
    ``sort_keys`` because ``jsonb`` has no key order of its own to preserve,
    so sorting is what makes two exports of one row byte-identical."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def encode_value(value: Any) -> Any:
    """Encode one already-SQLite-shaped column value for JSON.

    ``bool`` is checked before the byte types and folded to 0/1 because that
    is the SQLite shape -- and because a PostgreSQL ``boolean`` column hands
    psycopg a real ``bool``, which would otherwise travel as JSON
    ``true``/``false`` and reach a SQLite target as a different literal than
    every other environment writes.

    This handles only what JSON cannot carry natively. Timestamp and JSON
    columns are the caller's business, because both need to know WHICH
    column a value came from -- see ``column_encoder``.
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


def encode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {name: encode_value(value) for name, value in row.items()}


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    return {name: decode_value(value) for name, value in row.items()}


def column_encoder(
    json_columns: Collection[str], timestamp_columns: Collection[str]
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return ``row -> portable row`` for one table, keyed BY COLUMN.

    Three classes, which the caller decides from the migration DDL contract
    and never from the value: a JSON column travels as text
    (``json_column_text``); a timestamp column travels as UTC ISO text
    (``utc_timestamp_text``), with NULL staying NULL; everything else goes
    through ``encode_value`` for the two shapes JSON cannot carry natively,
    bytes and bool.
    """
    json_set = frozenset(json_columns)
    timestamp_set = frozenset(timestamp_columns)

    def encode(row: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in row.items():
            if name in timestamp_set:
                result[name] = utc_timestamp_text(value)
            elif name in json_set:
                result[name] = json_column_text(value)
            else:
                result[name] = encode_value(value)
        return result

    return encode


# ------------------------------------------------------------- path safety

# A single filesystem path SEGMENT drawn from file content that will be
# spliced into a real path (a notebook id becoming a directory name, mainly).
# Deliberately narrow -- this is not "what a notebook id happens to look
# like today", it is "what is safe to join onto a trusted base path without
# a traversal or a shell-metacharacter surprise". Kept in this module because
# the SAME rule has to hold for whatever a writer puts into a manifest and
# whatever its reader takes back out -- one rule, not two that can drift.
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
    """True when ``value`` can be ONE segment of a file-relative path.

    Looser than ``is_safe_identifier`` on purpose: the last segment of a
    ``files/**`` path is an uploaded file's name, which ``stored_upload_name``
    keeps as the user typed it -- ``研究.pdf``, ``meeting notes.pdf`` -- so a
    character whitelist here would reject files the exporter itself
    produced (codex #772 r10). What a segment must NOT be is what lets it
    leave its directory: empty, ``.``/``..``, or carrying a separator or NUL.
    Containment is proven separately by resolving the path against its root."""
    if not value or value in (".", ".."):
        return False
    return "/" not in value and "\\" not in value and "\x00" not in value


def is_safe_relative_path(value: str) -> bool:
    """True when ``value`` is a relative, ``/``-separated path whose every
    segment is ``is_safe_path_segment``. Refuses an empty string, a leading
    ``/`` or ``\\`` (absolute on POSIX or Windows), and a Windows drive prefix
    (``C:...``) -- and, through the per-segment check, any ``..`` hop.
    Notebook and package ids are held to the stricter ``is_safe_identifier``
    by the importers' preflight; this function only guards traversal."""
    if not value or value.startswith("/") or value.startswith("\\"):
        return False
    if len(value) >= 2 and value[1] == ":":
        return False
    return all(is_safe_path_segment(segment) for segment in value.split("/"))


# ------------------------------------------------------------------ digests


def sha256_hex(data: bytes) -> str:
    """The lowercase hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


class DigestCounter:
    """A running sha256 plus byte count over everything ``update`` saw: what a
    streaming writer records for an entry it never holds in memory whole, and
    what its reader recomputes to prove the entry was not altered."""

    __slots__ = ("_digest", "size")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.size = 0

    def update(self, data: bytes) -> None:
        self._digest.update(data)
        self.size += len(data)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()
