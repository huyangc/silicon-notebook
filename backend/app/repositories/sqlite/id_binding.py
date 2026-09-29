"""The single way a SQLite statement binds a potentially large id list.

A source ceiling (the frozen list of source ids an asker may read) can hold a
whole notebook's visible sources -- tens of thousands of ids.  Two hazards were
measured on the statements that used to bind it (plan
``2026-09-29-ceiling-sql-hazards.md``):

* **Variable limit.**  One ``?`` per id makes the statement's parameter count
  grow with the notebook.  Deployment builds of SQLite cap it at 32,766
  (``SQLITE_LIMIT_VARIABLE_NUMBER``; see ``core/cache/sqlite_backend.py``); a
  larger ceiling raises ``OperationalError``, which a fail-open caller turns
  into a silently empty channel.  The local conda build allows 250,000, so the
  defect never shows up on a development machine.
* **Per-id seek.**  ``col IN (<list>)`` on an indexed column lets the planner
  drive the lookup by the list: inside a correlated ``EXISTS`` against the
  composite index ``(object_id, source_id)`` that is one probe per ceiling id
  per candidate row (6.6-8.7 s at 49k ids).  A unary ``+`` on the column
  removes it from index consideration, so the probe goes by the correlated key
  alone and the list is a membership test (14-20 ms at 49k ids).

Rules:

1. A ceiling always occupies exactly **one** ``?``: a JSON array produced by
   :func:`ids_param` and unpacked by :data:`JSON_IDS`.  Never
   ``",".join("?" for ...)`` over a ceiling.
2. The default is :func:`member_of` / :func:`not_member_of` -- the statement is
   driven by its other, selective predicates and the list only filters.
3. :func:`drive_by` is for the statements whose intent really is "seek every
   listed id" (e.g. every chunk of these sources).  Its name is the review
   signal: each use must be justified at the call site.
4. Exempt: small bounded lists that the caller already batches (the service
   layer's ``_in_batches`` windows of at most 900 candidate primary keys, the
   element-id windows of at most 64).  Their size does not grow with the
   notebook, they stay far below the variable limit, and seeking by those
   primary keys is exactly the plan wanted, so plain placeholders are fine.

:data:`JSON_IDS` casts every element to TEXT.  Ids are stored as TEXT; a caller
passing an integer id got a match under the old placeholder form (the column's
TEXT affinity converted the bound integer), and a JSON number would not compare
equal to a TEXT value once the unary ``+`` has stripped the column's affinity.
The cast keeps both spellings matching, the same way PostgreSQL's ``text[]``
binding does.
"""
from __future__ import annotations

import json
from collections.abc import Iterable


JSON_IDS = "(SELECT CAST(value AS TEXT) FROM json_each(?))"


def member_of(column: str) -> str:
    """``+column IN JSON_IDS``: filter by the list without letting it drive."""
    return f"+{_expression(column)} IN {JSON_IDS}"


def not_member_of(column: str) -> str:
    """``+column NOT IN JSON_IDS``: the exclusion twin of :func:`member_of`.

    :func:`ids_param` never emits ``null``, so ``NOT IN`` cannot turn into
    "unknown" for every row."""
    return f"+{_expression(column)} NOT IN {JSON_IDS}"


def drive_by(column: str) -> str:
    """``column IN JSON_IDS``: the list drives the plan (one seek per id).

    Only where that is the intent; see rule 3 of the module docstring."""
    return f"{_expression(column)} IN {JSON_IDS}"


def ids_param(values: Iterable[str | int]) -> str:
    """The JSON array bound to the single ``?`` of :data:`JSON_IDS`.

    De-duplicated, ``None`` and blank strings dropped, deterministic: a list or
    tuple keeps its first-seen order, a set or frozenset is sorted (its
    iteration order depends on the per-process string hash seed).  A ``str``
    raises ``TypeError``: iterating it would bind its characters as ids and
    silently read the wrong rows.  Integer ids are kept as JSON numbers;
    :data:`JSON_IDS` casts them back to TEXT.
    """
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(
            "ids_param() takes a collection of ids, not a single string"
        )
    unique = dict.fromkeys(values)
    # Fast path for the common all-``str`` ceiling (tens of thousands of ids,
    # once per statement): one comprehension, no per-item function call.
    kept: list[str | int] = [
        value for value in unique
        if type(value) is str and value and not value.isspace()
    ]
    if len(kept) != len(unique):
        kept = [value for value in map(_clean, unique) if value is not None]
    if isinstance(values, (set, frozenset)):
        kept.sort(key=lambda value: (isinstance(value, int), value))
    return json.dumps(kept, ensure_ascii=False)


def _clean(value: object) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value and not value.isspace() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"id must be str or int, got {type(value).__name__}")


def _expression(column: str) -> str:
    if not isinstance(column, str) or not column.strip():
        raise ValueError("an id-list predicate needs a column expression")
    return column
