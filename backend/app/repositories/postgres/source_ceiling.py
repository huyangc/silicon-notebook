"""The one PostgreSQL home for honouring a per-run SOURCE CEILING in SQL.

``sqlite/source_ceiling.py`` is the mirror image (same names, same
semantics, dialect differs).  A source ceiling is the set of sources the asker
ticked or may read (``services/source_scope.scoped_allowed_source_ids``):
``None`` = no ceiling, empty = deny all.

Bind or not — the rule
======================

* A statement that must filter BELOW a ``LIMIT`` — a keyset page, a count
  that is the denominator of those pages — binds the ceiling through this
  module: ``normalise_ceiling`` → ``ceiling_param`` → ``evidence_support_sql``
  → ``execute_with_ceiling``.  Filtering such a statement after the fact would
  starve its pages; its candidate set is a whole library.
* Code that already holds a small, bounded candidate set (a cluster's member
  sources, a handful of sibling procedures) reads the candidates WITHOUT the
  ceiling and tests membership in Python against the ``normalise_ceiling``
  frozenset: nothing to bind, nothing to plan.

What binding costs, and why it is written the way it is
=======================================================

Measured on PostgreSQL 16, 30k objects per notebook, certified reverse index,
one 25-row page, 49k-id ceiling (``tests/postgres/
test_kg_enumeration_ceiling_explain_pins.py`` pins the resulting plans):

* A Python ``list`` parameter costs ~21 ms of pure client CPU (text array
  adaptation, GIL held; binary ``%b`` still ~9 ms).  ``ceiling_param`` binds
  ONE text value instead — the ids joined by ``\\x1f`` — and the statement
  unfolds it with ``string_to_array(%s, E'\\x1f')``.  With a custom plan the
  immutable call is constant-folded into a hashed ``= ANY('{...}'::text[])``,
  exactly what the list produced, for ~2 ms of transfer.  An id containing
  the separator falls back to a binary array (``%b``), never to a split.
* The planner charges the hashed array's build once per inner rescan and
  answers with Gather Merge + 2 workers, each receiving and hashing the whole
  constant (~20 ms, a parallel-worker slot per page).  ``execute_with_ceiling``
  turns parallelism off for that one statement.
* psycopg prepares a statement on its fifth execution per connection, after
  which PostgreSQL may choose a GENERIC plan: the ceiling becomes an opaque
  ``$n`` (assumed ~10 elements, unhashed) and, e.g., the uncertified count went
  from ~40 ms to ~3.6 s at its sixth execution.  ``execute_with_ceiling``
  always sends the unnamed (unprepared) statement.

Together (same fixture, one connection, median of 15, machine under load):
dense 49k page 65 → 9 ms with the scope's frozenset (14 ms when a fresh list
is normalised per call), dense 4k page 11 → 2 ms, dense 49k count 50 → 26 ms.
Planning the folded 49k constant (~6 ms) is now most of the page.  The
trade-off: a page that scans a whole type (nothing or little inside the
ceiling) no longer gets parallel workers — none-4k page 27 → 28 ms, sparse-49k
89 → 36 ms, i.e. it never lost what the workers' copies of the constant cost.
The ceiling is still one bind per statement; normalise it once per run (hand
the scope's own frozenset down) so ``ceiling_param``'s cache serves every page.

Two layers, and where they will live
====================================

This module is written as two layers so the binding half can move to the
repository-wide id-binding home (``postgres/id_binding.py``, branch
``claude/ceiling-sql-binding``, merging first) by mechanical replacement:

* CEILING layer — stays here: ``normalise_ceiling``, ``EVIDENCE_ITEM_SOURCE``,
  ``ATTRIBUTABLE_SOURCE``, ``evidence_items``, ``evidence_source_exists``,
  ``evidence_support_sql`` and the identity cache inside ``ceiling_param``
  (it is keyed by the run's ceiling object, a ceiling concern).
* BINDING layer — delegates after that merge: the body of ``ceiling_param``
  below the cache (joined text / ``%b`` fallback, i.e. the ``BoundCeiling``
  it builds) becomes that module's id-list parameter builder, and
  ``_run`` becomes its ``execute_ids`` (always ``prepare=False``).  The
  statement-local ``_STATEMENT_SETTINGS`` stay here unless that module owns
  an equivalent, in which case ``_apply_settings`` / ``_restore_settings``
  are deleted and ``execute_with_ceiling`` becomes a thin alias.
  ``BoundCeiling`` is only the pair (SQL expression, value); any builder that
  returns such a pair drops in without touching ``evidence_support_sql`` or
  the stores.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Iterable, NamedTuple, Optional, Sequence

from psycopg.pq import TransactionStatus

SEPARATOR = "\x1f"

# The source id of one evidence item ``ev`` (a jsonb element from
# ``evidence_items``); non-object items yield NULL.
EVIDENCE_ITEM_SOURCE = "ev->>'source_id'"
# ``ev`` names a source at all (empty / missing ``source_id`` names none —
# the same rule ``source_ids_from_evidence`` applies to the reverse index).
ATTRIBUTABLE_SOURCE = f"COALESCE({EVIDENCE_ITEM_SOURCE},'')<>''"

# ``execute_with_ceiling``'s statement-local settings.  ``jit=off`` was added
# on the planner's ESTIMATE (the uncertified count's top cost, ~3.7M, is above
# the default ``jit_above_cost`` and both inline/optimize thresholds), not on
# a measurement: the development servers have no LLVM.  Where JIT is
# unavailable the setting is a no-op.
_STATEMENT_SETTINGS = (("max_parallel_workers_per_gather", "0"), ("jit", "off"))

_CACHE_LIMIT = 8
_cache_lock = threading.Lock()
_cache: "OrderedDict[int, tuple[frozenset, BoundCeiling]]" = OrderedDict()


class BoundCeiling(NamedTuple):
    """A ceiling in bindable form: ``sql`` is the text array expression to put
    inside ``= ANY(...)`` / ``<> ALL(...)`` (it holds exactly ONE placeholder),
    ``value`` the one parameter it binds."""

    sql: str
    value: Any


def normalise_ceiling(source_ids: Optional[Iterable[str]]) -> Optional[frozenset]:
    """The ONE normalisation of a source-id set.

    ``None`` stays ``None`` (no ceiling).  A ``frozenset`` without empty ids is
    returned as is — the scope's own memoised ceiling is never copied, which
    is what lets ``ceiling_param`` recognise it.  Any other iterable becomes a
    frozenset with blanks dropped, so an empty result (``()``, ``("",)``) means
    deny all, never "unrestricted".  A ``str`` raises ``TypeError``: it is an
    iterable of characters, and ``"src-ab12"`` would otherwise silently become
    the ceiling ``{"s", "r", "c", ...}``.
    """
    if source_ids is None:
        return None
    if isinstance(source_ids, (str, bytes)):
        raise TypeError("a source ceiling is a collection of ids, not one string")
    if (
        isinstance(source_ids, frozenset)
        and "" not in source_ids
        and None not in source_ids
    ):
        return source_ids
    return frozenset(str(value) for value in source_ids if value)


def ceiling_param(ceiling: frozenset) -> BoundCeiling:
    """The bound form of a NON-EMPTY normalised ceiling, computed once per
    ceiling object.

    Keyed by the frozenset's identity with a small LRU bound.  Identity is safe
    here because a frozenset cannot change after it is built, and each entry
    holds a strong reference to its key object: while the entry lives, no
    other object can be allocated at that address, and the ``is`` check makes
    a stale id harmless anyway.  A run hands every page the scope's one
    memoised frozenset, so one join serves the whole enumeration; a caller that
    passes a fresh collection per call simply misses (the result is the same,
    only recomputed).
    """
    key = id(ceiling)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] is ceiling:
            _cache.move_to_end(key)
            return hit[1]
    if any(SEPARATOR in value for value in ceiling):
        bound = BoundCeiling("%b", list(ceiling))
    else:
        bound = BoundCeiling("string_to_array(%s, E'\\x1f')", SEPARATOR.join(ceiling))
    with _cache_lock:
        _cache[key] = (ceiling, bound)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return bound


def evidence_items(ref: str) -> str:
    """``ref``'s evidence array expanded as ``ev`` (a non-array counts as
    empty)."""
    return (
        "jsonb_array_elements(CASE WHEN jsonb_typeof("
        f"{ref}.evidence)='array' THEN {ref}.evidence ELSE '[]'::jsonb END) ev"
    )


def evidence_source_exists(ref: str, condition: str) -> str:
    """``ref`` has at least one evidence item satisfying ``condition`` (written
    on ``ev``)."""
    return f"EXISTS (SELECT 1 FROM {evidence_items(ref)} WHERE {condition})"


def evidence_support_sql(ref: str, bound: BoundCeiling, *, authoritative: bool) -> str:
    """The ONLY text of the support predicate: the ``knowledge_objects`` row
    ``ref`` (an alias or the unaliased table name) has at least one evidence
    item whose source is in ``bound``.  Binds ``bound.value`` once.

    Support is EVIDENCE (``evidence[].source_id``), never the row's own
    ``source_id`` column (its owner): a merged object's evidence spans several
    sources.  ``authoritative`` = the P0-4 reverse index is NOT certified
    (``source_index_backfilled`` False means history unknown, not "no rows"),
    so the row's evidence JSON is read; otherwise ``knowledge_object_sources``
    answers with one ``idx_kos_object`` probe per candidate row.
    """
    if authoritative:
        return evidence_source_exists(ref, f"{EVIDENCE_ITEM_SOURCE}=ANY({bound.sql})")
    return (
        "EXISTS (SELECT 1 FROM knowledge_object_sources kos "
        f"WHERE kos.object_id={ref}.id "
        f"AND kos.notebook_id={ref}.notebook_id "
        f"AND kos.source_id=ANY({bound.sql}))"
    )


def execute_with_ceiling(db: Any, sql: str, params: Sequence[Any]) -> Any:
    """Execute a statement that binds a ceiling: unprepared, with parallel
    query (and JIT) off for exactly this statement.

    The settings are transaction-local (``set_config(..., true)``) and are put
    back to their previous values right after the statement, so nothing
    reaches the caller's next statement on the same connection — pooled read
    connections are not autocommit, and a caller's transaction routinely
    spans many statements.  The caller's transaction itself is untouched: no
    BEGIN, COMMIT or ROLLBACK is issued on a non-autocommit connection.  If
    the statement fails, the transaction is aborted and the caller's
    rollback discards the settings with it.  On an autocommit connection
    (outside the pool) ``SET LOCAL`` would be a no-op, so the three statements
    run in one short explicit transaction instead.
    """
    if getattr(db, "autocommit", False):
        with db.transaction():
            _apply_settings(db)
            return _run(db, sql, params)
    previous = _apply_settings(db)
    try:
        return _run(db, sql, params)
    finally:
        info = getattr(db, "info", None)
        if info is None or info.transaction_status == TransactionStatus.INTRANS:
            _restore_settings(db, previous)


def _run(db: Any, sql: str, params: Sequence[Any]) -> Any:
    return db.execute(sql, tuple(params), prepare=False)


def _apply_settings(db: Any) -> tuple:
    # The MATERIALIZED CTE reads the previous values before the outer target
    # list writes the new ones: one round trip, a defined order.
    reads = ", ".join(
        f"current_setting('{name}') AS p{index}"
        for index, (name, _value) in enumerate(_STATEMENT_SETTINGS)
    )
    kept = ", ".join(f"p{index}" for index in range(len(_STATEMENT_SETTINGS)))
    writes = ", ".join(
        f"set_config('{name}', '{value}', true)" for name, value in _STATEMENT_SETTINGS
    )
    row = db.execute(
        f"WITH previous AS MATERIALIZED (SELECT {reads}) "
        f"SELECT {kept}, {writes} FROM previous"
    ).fetchone()
    values = list(row.values()) if isinstance(row, dict) else list(row)
    return tuple(values[: len(_STATEMENT_SETTINGS)])


def _restore_settings(db: Any, previous: tuple) -> None:
    db.execute(
        "SELECT " + ", ".join(
            f"set_config('{name}', %s, true)" for name, _value in _STATEMENT_SETTINGS
        ),
        previous,
    )
