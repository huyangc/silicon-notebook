"""The one way a PostgreSQL statement binds an id list that can be large.

Rule
----
A statement that filters by an id list whose size is set by the data (a run's
frozen source ceiling can hold every source of a notebook, ~49 000 ids) binds
that list through :func:`bind_ids` and runs through :func:`execute_ids`.

Why
---
psycopg prepares a statement on its 5th execution on a connection; from about
the 11th execution PostgreSQL's plan cache switches that statement to a
GENERIC plan.  In a generic plan the array is an opaque parameter: estimated
at 10 elements, not hashed, and used as an index condition.  Measured on a
49 000-source notebook (PG 16, 2026-09-29): chunk FTS 1.1 s -> 3.1 s (beyond
its 3 s private timeout; the timeout's ``ROLLBACK TO SAVEPOINT`` then clears
psycopg's prepared cache, so the cycle repeats every ~11 executions), a rare
term 7 ms (custom plan) vs 7.7 s (generic), the KG authoritative gate
169 ms -> 6.7 s, contribution hydration 78 -> 293 ms.  ``prepare=False``
makes every execution an unnamed one-shot statement, which PostgreSQL always
plans with the real values (a custom plan): the array is a constant, hashed,
and estimated at its true length.  The price is planning on every execution
(a few ms), which these statements already paid on executions 1-5.

:func:`bind_ids` also sends the list as ONE text parameter split by the
planner, ``string_to_array(%s,E'\\x1f')``, instead of a Python list.  psycopg
spends ~17 ms of GIL-held CPU converting a 49 000-element list to a binary
text array; joining the ids costs ~0.3 ms.  In a custom plan
``string_to_array`` over a constant folds to the same constant ``text[]``, so
the plan and the rows are those of the array form.  The text form is used only
when it is exact: every id is a non-empty ``str`` without the separator.
Otherwise the list is bound exactly as before (one array parameter) — an
empty id would split into zero elements, a separator would split one id in
two, and a non-string (``None``) has array-NULL semantics the text form
cannot express.

Measured and NOT adopted: statement-local ``jit = off`` /
``max_parallel_workers_per_gather = 0``.  No converted statement reached the
JIT threshold, and the one that plans a parallel Gather (generated-question
rows) got slower without workers (4k ids 22 -> 36 ms, 49k 119 -> 132 ms).

Exempt
------
Lists bounded by construction — a primary-key hydration window (<= a few
hundred ids), a caller batch of <= 900, element ids <= 64, notebook ids <= 8 —
keep a plain ``execute``: their generic plan is as good as the custom one and
they benefit from the plan cache.  A statement that binds no list keeps its
execute call byte-identical (it must not pay the per-execution planning).

A fragment from :func:`bind_ids` must only ever run through
:func:`execute_ids`: under a prepared generic plan ``string_to_array($n)`` is
as opaque as the array it replaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


ID_SEPARATOR = "\x1f"
_TEXT_ARRAY_SQL = "string_to_array(%s,E'\\x1f')"
_ARRAY_SQL = "%s"


@dataclass(frozen=True)
class BoundIds:
    """One bound parameter plus the SQL expression that turns it into ``text[]``.

    ``array_sql`` holds exactly one ``%s``; put it where the statement used to
    say ``%s`` inside ``ANY(...)`` / ``ALL(...)`` and append ``param`` at that
    position of the parameter list.
    """

    array_sql: str
    param: object


def bind_ids(ids: Sequence[Any]) -> BoundIds:
    """Bind ``ids`` (order and duplicates preserved) as one array parameter."""
    values = list(ids)
    if all(
        isinstance(value, str) and value and ID_SEPARATOR not in value
        for value in values
    ):
        return BoundIds(_TEXT_ARRAY_SQL, ID_SEPARATOR.join(values))
    return BoundIds(_ARRAY_SQL, values)


def execute_ids(connection: Any, statement: str, params: Sequence[Any]):
    """Execute a statement that binds a large id list, always as a custom plan.

    ``prepare=False`` is a per-call psycopg option; every connection wrapper on
    the read paths forwards keyword arguments unchanged (the read-budget
    wrapper still sets its per-statement deadline first, and savepoints and
    private ``statement_timeout`` settings are separate statements the owning
    store issues around this one).
    """
    return connection.execute(statement, params, prepare=False)
