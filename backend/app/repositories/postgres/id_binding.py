"""How a PostgreSQL statement binds an id list whose size is set by the data.

The rule (stated in the same terms in ``sqlite/id_binding.py`` and in
``docs/development.md``, "Binding id lists in SQL"):

    A statement that binds an id list whose size is set by the data -- above
    all a run's frozen source ceiling, which can hold every source of a
    notebook (~49 000 ids) -- binds it through this module: one parameter from
    :func:`bind_ids`, a predicate from :func:`member_of` /
    :func:`not_member_of`, executed through :func:`execute_ids` (or
    :func:`execute_bound`).  Only the reviewed exemption classes below may
    bind a list directly; ``tests/test_id_list_binding_guard.py`` lists every
    such site with its class and fails on any other.

Why
---
psycopg prepares a statement on its 5th execution on a connection; from about
the 11th execution PostgreSQL's plan cache may switch it to a GENERIC plan, in
which a bound array is an opaque parameter: estimated at 10 elements, not
hashed, and free to become an index condition.  Measured on a 49 000-source
notebook (PG 16, 2026-09-29): chunk FTS 1.1 s -> 3.1 s from the 11th
execution, past its 3 s private timeout (whose ``ROLLBACK TO SAVEPOINT`` then
clears psycopg's prepared cache, so the cycle repeats); a rare term 7 ms
(custom plan) vs 7.7 s (generic); the KG authoritative gate 169 ms -> 6.7 s;
contribution hydration 78 -> 293 ms.  :func:`execute_ids` sends
``prepare=False``: every execution is an unnamed one-shot statement that
PostgreSQL plans with the real values (a custom plan) -- the list is a
constant, hashed, and costed at its true length.

What a custom plan costs
------------------------
Planning on every execution, and the bill depends on the column's
statistics.  For ``col = ANY(<constant>)`` the planner estimates selectivity
element by element against the column's most-common values.  A 49 000-id
ceiling plans in 5-11 ms on a column without MCVs (uniform ids).  On a column
WITH MCVs -- ``chunks.source_id`` in a real notebook, where chunks per source
are skewed; 100 MCVs -- the same ceiling plans in 62-81 ms (135-213 ms
measured again on a machine at load 25-40).  On the rare-term chunk FTS that
is about 24 ms per execution slower than the generic plan, which does not
regress there, i.e. about 0.2 s per question at nine chunk searches.  The
trade is a bounded, predictable planning cost in place of a cliff of seconds.

Forms
-----
* :func:`member_of` -- ``col = ANY(list)``, the list filters rows something
  else drives, including correlated per-row probes;
* :func:`not_member_of` -- ``col <> ALL(list)``, the exclusion twin.

Measured and not adopted: the semi-join form ``col IN (SELECT unnest(list))``,
which is planned from the list's length alone and so skips the per-element
MCV pass.  It was measured for every converted statement on uniform and on
skewed statistics (1st/6th/15th execution end to end, and planning plus
execution under EXPLAIN ANALYZE).  It plans in 1-9 ms everywhere, but the
planner then drives by the list: chunk FTS common term 1.8 s -> 5-7 s at
49k ids (past the timeout), KG reverse-index gate 0.67 -> 0.97 s (skewed) and
109 -> 440 ms (uniform), generated-question rows 273 -> 633 ms, peers
38 -> 120 ms, ``ids_for_sources`` 27 -> 110 ms, the KG authoritative gate
8.7 s.  Contribution hydration came closest (skewed 49k 218 -> 28 ms) but lost
on uniform statistics (49k 7.5 -> 11.5 ms, 4k 0.8 -> 1.2 ms).  No statement
was no worse in every cell, so none uses it and the module does not offer it.

:func:`bind_ids` sends the list as ONE text parameter split by the planner,
``string_to_array(%s,E'\\x1f')``, instead of a Python list: psycopg spends
~17 ms of GIL-held CPU converting 49 000 strings to a binary array, joining
them costs ~0.3 ms, and in a custom plan ``string_to_array`` over a constant
folds to the same constant ``text[]``.  The text form is used only when it is
exact; a list containing an empty id or the separator is bound as a
``text[]`` array parameter instead (an empty id would split into zero
elements, a separator would split one id in two).

The binding layer never changes membership: ids are bound exactly as given
(order, duplicates, blank ids), and a non-string id raises ``TypeError``
rather than being coerced.  Normalising a list (de-duplication) is the
caller's decision, made once where the list is built, and the SQLite and
PostgreSQL stores make the same one.

Not adopted, deliberately
-------------------------
* A complement list ("bind the few unticked ids when nearly everything is
  ticked").  A frozen ceiling must not admit sources added after the freeze;
  an exclusion list would admit every one of them.
* An identity cache of bound payloads.  A cache keyed by list identity
  belongs to the caller that owns a long-lived list object; every store call
  here receives a freshly built list, so a cache here could never hit.
* Statement-local planner settings (``jit = off``,
  ``max_parallel_workers_per_gather = 0``).  Their effect was measured in
  both directions (generated-question rows got slower without parallel
  workers: 4k ids 22 -> 36 ms, 49k 119 -> 132 ms), so they are not a default
  of this module; a caller that wants them sets them around its own
  statement.

Exemption classes (the same three on both backends)
---------------------------------------------------
1. **Bounded by construction** -- the method or its caller caps the list at a
   size that does not grow with the notebook: a page or ``LIMIT`` window, a
   ranked-candidate primary-key window, one run's notebook ids (<= 8), a
   fixed set of statuses or kinds, one source's element ids.
2. **Batched key probe** -- the ids are the keys of the rows the statement
   exists to read or write, the list is its only selective predicate, and the
   list is cut into batches (PostgreSQL <= 1024 ids, SQLite <= 900
   placeholders).  Every plan probes each key, so neither the plan cache nor
   the size estimate can move the plan off that index (at worst it chooses
   between an index and a bitmap scan of it).
3. **Driven by the list, one parameter** -- the whole list is the set of keys
   to read, bound as one parameter (an array or ``jsonb``), and the statement
   has no other selective predicate, so the list drives every plan by design
   (``visible_source_scope_snapshot``, one community's or cluster's members).

A list that FILTERS a statement driven by something else (a text search, an
ordering under ``LIMIT``, a correlated probe, a small primary-key window) is
never exempt.  A statement that binds no list keeps its execute call
byte-identical and so keeps the plan cache.
"""
from __future__ import annotations

from typing import Any, NamedTuple, Sequence


ID_SEPARATOR = "\x1f"
_TEXT_ARRAY_SQL = "string_to_array(%s,E'\\x1f')"
_ARRAY_SQL = "%s::text[]"


class BoundIds(NamedTuple):
    """One bound parameter plus the SQL that turns it into ``text[]``.

    ``sql`` holds exactly one ``%s``; the predicate helpers place it, and the
    caller appends ``param`` at that position of the parameter list.  Same
    shape as the SQLite module's ``BoundIds``.
    """

    sql: str
    param: object


def bind_ids(ids: Sequence[str]) -> BoundIds:
    """Bind ``ids`` exactly as given (order, duplicates, blanks) as one parameter."""
    if isinstance(ids, (str, bytes, bytearray)):
        raise TypeError("bind_ids() takes a collection of ids, not a single string")
    values = list(ids)
    exact = True
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"id must be str, got {type(value).__name__}")
        if exact and (not value or ID_SEPARATOR in value):
            exact = False
    if exact:
        return BoundIds(_TEXT_ARRAY_SQL, ID_SEPARATOR.join(values))
    return BoundIds(_ARRAY_SQL, values)


def member_of(column: str, bound: BoundIds) -> str:
    """``column = ANY(list)``: the membership test."""
    return f"{_expression(column)}=ANY({bound.sql})"


def not_member_of(column: str, bound: BoundIds) -> str:
    """``column <> ALL(list)``: the exclusion twin of :func:`member_of`."""
    return f"{_expression(column)}<>ALL({bound.sql})"


def execute_ids(connection: Any, statement: str, params: Sequence[Any]):
    """Execute a statement that binds an id list, always as a custom plan.

    ``prepare=False`` is a per-call psycopg option; every connection wrapper on
    the read paths forwards keyword arguments unchanged (the read-budget
    wrapper still sets its per-statement deadline first, and savepoints and
    private ``statement_timeout`` settings are separate statements the owning
    store issues around this one).
    """
    return connection.execute(statement, params, prepare=False)


def execute_bound(
    connection: Any, statement: str, params: Sequence[Any],
    bound: BoundIds | None,
):
    """:func:`execute_ids` when ``bound`` is a list binding, else the plain
    ``connection.execute(statement, params)`` -- a statement that binds no
    list keeps the plan cache."""
    if bound is None:
        return connection.execute(statement, params)
    return execute_ids(connection, statement, params)


def _expression(column: str) -> str:
    if not isinstance(column, str) or not column.strip():
        raise ValueError("an id-list predicate needs a column expression")
    return column
