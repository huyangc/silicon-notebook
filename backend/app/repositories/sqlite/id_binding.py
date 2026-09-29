"""How a SQLite statement binds an id list whose size is set by the data.

The rule (stated in the same terms in ``postgres/id_binding.py`` and in
``docs/development.md``, "Binding id lists in SQL"):

    A statement that binds an id list whose size is set by the data -- above
    all a run's frozen source ceiling, which can hold every source of a
    notebook (~49 000 ids) -- binds it through this module: one parameter from
    :func:`bind_ids`, a predicate from :func:`member_of` /
    :func:`not_member_of` / :func:`drive_by`.  Only the reviewed exemption
    classes below may bind a list directly;
    ``tests/test_id_list_binding_guard.py`` lists every such site with its
    class and fails on any other.

Why
---
Two hazards were measured on the statements that used to bind a ceiling:

* **Variable limit.**  One ``?`` per id makes the statement's parameter count
  grow with the notebook.  Deployment builds of SQLite cap it at 32,766
  (``SQLITE_LIMIT_VARIABLE_NUMBER``; see ``core/cache/sqlite_backend.py``); a
  larger list raises ``OperationalError``, which a fail-open caller turns into
  a silently empty channel.  The local conda build allows 250,000, so the
  defect never shows up on a development machine.
* **Per-id seek.**  ``col IN (<list>)`` on an indexed column lets the planner
  drive the lookup by the list: inside a correlated ``EXISTS`` against the
  composite index ``(object_id, source_id)`` that is one probe per listed id
  per candidate row (6.6-8.7 s at 49k ids).  A unary ``+`` on the column
  removes it from index consideration, so the probe goes by the correlated key
  alone and the list is a membership test (14-20 ms at 49k ids).

Production SQLite databases carry no planner statistics (the repository never
runs ``ANALYZE``), so a plan must be right WITHOUT ``sqlite_stat1`` as well as
with it; the plan pins run in both states.  Where the notebook predicate would
otherwise win the index choice in the statistics-free state, it is written
``+<alias>.notebook_id = ?`` so the intended key drives.

Forms
-----
* :func:`bind_ids` -- ``BoundIds(sql, param)``: :data:`JSON_IDS` and one JSON
  array.  A ceiling therefore always occupies exactly one ``?``.
  ``sort=True`` writes the array sorted for the membership forms (a cheaper
  ``IN``-list build; see the function).
* :func:`member_of` / :func:`not_member_of` -- ``+col IN / NOT IN (list)``, the
  default: the statement is driven by its other, selective predicates and the
  list only filters.
* :func:`drive_by` -- ``col IN (list)``, for statements whose intent is "seek
  every listed id" (every chunk of these sources).  Its name is the review
  signal: each use says at the call site why the list should drive, and its
  plan pin shows that it does in both statistics states.

The binding layer never changes membership: ids are bound exactly as given
(order, duplicates, empty or blank ids), and a non-string id raises
``TypeError`` rather than being coerced.  Normalising a list (de-duplication,
dropping blanks) is the caller's decision, made once where the list is built,
and the SQLite and PostgreSQL stores make the same one.

Not adopted, deliberately: a complement list (binding the few unticked ids
when nearly everything is ticked) -- a frozen ceiling must not admit sources
added after the freeze, and an exclusion list would; and an identity cache of
payloads, which belongs to the caller that owns a long-lived list object.

Exemption classes (the same three on both backends)
---------------------------------------------------
1. **Bounded by construction** -- the method or its caller caps the list at a
   size that does not grow with the notebook: a page or ``LIMIT`` window, a
   ranked-candidate primary-key window, one run's notebook ids (<= 8), a
   fixed set of statuses or kinds, one source's element ids.
2. **Batched key probe** -- the ids are the keys of the rows the statement
   exists to read or write, the list is its only selective predicate, and the
   list is cut into batches (SQLite <= 900 placeholders, PostgreSQL <= 1024
   ids).  Batching keeps the statement far below the variable limit and
   seeking by those keys is exactly the plan wanted.
3. **Driven by the list, one parameter** -- the whole list is the set of keys
   to read, bound as one JSON parameter, and the statement has no other
   selective predicate, so the list drives every plan by design
   (``visible_source_scope_snapshot``, which also needs each id's ordinal).

A list that FILTERS a statement driven by something else (a text search, an
ordering under ``LIMIT``, a correlated probe, a small primary-key window) is
never exempt.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import NamedTuple


JSON_IDS = "(SELECT value FROM json_each(?))"


class BoundIds(NamedTuple):
    """One bound parameter plus the SQL that unpacks it (same shape as the
    PostgreSQL module's ``BoundIds``)."""

    sql: str
    param: str


def bind_ids(ids: Iterable[str], *, sort: bool = False) -> BoundIds:
    """Bind ``ids`` exactly as given as one JSON array.

    A list or tuple keeps its order; a set or frozenset is sorted (its
    iteration order depends on the per-process string hash seed).  A single
    ``str`` raises ``TypeError`` -- iterating it would bind its characters --
    and so does any non-string id.

    ``sort=True`` writes the array in sorted order, for the membership forms
    (:func:`member_of` / :func:`not_member_of`): SQLite builds an ``IN``
    list's ephemeral index from ``json_each`` in array order, and sequential
    inserts are far cheaper than scattered ones.  Measured on a 49k-source
    notebook with a shuffled 49k-id ceiling, in both statistics states: KG
    reverse-index gate 36 -> 19 ms, chunk FTS rare term 29 -> 11 ms,
    contribution hydration 29 -> 10 ms, comparison peers 38 -> 12 ms and
    70 -> 31 ms; sorting costs 0.5 ms for an already sorted list and 7 ms for
    a shuffled one.  Membership ignores order, so nothing else changes.  The
    driven form keeps the caller's order (``sort=False``).
    """
    if isinstance(ids, (str, bytes, bytearray)):
        raise TypeError("bind_ids() takes a collection of ids, not a single string")
    values = list(ids)
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"id must be str, got {type(value).__name__}")
    if sort or isinstance(ids, (set, frozenset)):
        values.sort()
    return BoundIds(JSON_IDS, json.dumps(values, ensure_ascii=False))


def member_of(column: str, bound: BoundIds) -> str:
    """``+column IN list``: filter by the list without letting it drive."""
    return f"+{_expression(column)} IN {bound.sql}"


def not_member_of(column: str, bound: BoundIds) -> str:
    """``+column NOT IN list``: the exclusion twin of :func:`member_of`.

    The JSON array never carries ``null`` (non-strings are refused), so
    ``NOT IN`` cannot turn into "unknown" for every row."""
    return f"+{_expression(column)} NOT IN {bound.sql}"


def drive_by(column: str, bound: BoundIds) -> str:
    """``column IN list``: the list drives the plan (one seek per id).

    Only where that is the intent; the call site says why."""
    return f"{_expression(column)} IN {bound.sql}"


def _expression(column: str) -> str:
    if not isinstance(column, str) or not column.strip():
        raise ValueError("an id-list predicate needs a column expression")
    return column
