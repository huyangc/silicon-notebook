"""The one SQLite home for honouring a per-run SOURCE CEILING in SQL.

Mirror image of ``postgres/source_ceiling.py`` — same names, same semantics;
that module's docstring holds the full rule ("bind or not") and the
measurements.  In short: a statement that must filter BELOW a ``LIMIT``
(keyset pages, their counts) binds the ceiling through this module; code that
holds a small bounded candidate set tests membership in Python against the
``normalise_ceiling`` frozenset.

The SQLite specifics:

* The ceiling is ONE parameter (``id_binding.bind_ids``), a JSON array
  unfolded with ``json_each(?)``: a ceiling is a whole library's visible
  sources, tens of thousands in production, past
  ``SQLITE_MAX_VARIABLE_NUMBER``.  The ``IN (SELECT … json_each(?))`` list is
  uncorrelated, so SQLite materialises it once per statement.
* ⚠ The predicate is ``id_binding.member_of`` — the NON-driving ``+col IN``
  form — never ``drive_by``.  On the reverse-index branch the unary ``+`` on
  ``kos.source_id`` is load-bearing: without it the planner seeks
  ``uq_knowledge_object_sources_sync_key (object_id, source_id)`` once PER
  CEILING ID for every candidate row — 20k objects, 49k-id ceiling: 110 ms for
  a dense page, 19.3 s for a sparse one, 19.4 s for a dense count.  With it
  the probe is by ``object_id`` alone and tests the materialised list: 14 ms /
  11 ms.  (On the evidence-JSON branch the ``+`` is inert: the expression has
  no index.)
* There is no plan cache or parallel query to steer, so
  ``execute_with_ceiling`` is a plain execute; it exists so both backends'
  stores read the same.

The binding layer is ``sqlite/id_binding.py`` (as in the PostgreSQL twin);
the ceiling layer — ``normalise_ceiling``, the evidence predicate text and
the identity cache in ``ceiling_param`` — is this module's.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Iterable, Optional, Sequence

from app.repositories.sqlite.id_binding import BoundIds, bind_ids, member_of

# The source id of one evidence item ``ev`` (a ``json_each`` row from
# ``evidence_items``); non-object items yield NULL.
EVIDENCE_ITEM_SOURCE = (
    "json_extract(CASE WHEN ev.type='object' THEN ev.value ELSE '{}' END,"
    "'$.source_id')"
)
# ``ev`` names a source at all (empty / missing ``source_id`` names none).
ATTRIBUTABLE_SOURCE = f"COALESCE({EVIDENCE_ITEM_SOURCE},'')<>''"

_CACHE_LIMIT = 8
_cache_lock = threading.Lock()
_cache: "OrderedDict[int, tuple[frozenset, BoundIds]]" = OrderedDict()


def normalise_ceiling(source_ids: Optional[Iterable[str]]) -> Optional[frozenset]:
    """Identical to the PostgreSQL twin: ``None`` → ``None``; a frozenset
    without empty ids is returned as is; any other iterable → frozenset with
    blanks dropped (empty = deny all); a ``str`` raises ``TypeError``."""
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


def ceiling_param(ceiling: frozenset) -> BoundIds:
    """The bound form (``id_binding.bind_ids``: one JSON array) of a
    NON-EMPTY normalised ceiling, computed once per ceiling object;
    identity-keyed small LRU for the reason the PostgreSQL twin gives
    (immutable key, strong reference held, ``is`` check).  JSON has no
    separator to collide with, so there is no fallback form.

    The ids are written SORTED (``sort=True``): SQLite builds the ``IN``
    list's ephemeral index from ``json_each`` in array order, and sequential
    inserts are much cheaper than the hash order a frozenset iterates in.
    Measured with a cached 49k-id ceiling on 30k objects: dense page 19 → 5 ms,
    sparse page 30 → 16 ms, dense count 37 → 21 ms; the sort itself (~9 ms) is
    paid once per ceiling object."""
    key = id(ceiling)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] is ceiling:
            _cache.move_to_end(key)
            return hit[1]
    bound = bind_ids(ceiling, sort=True)
    with _cache_lock:
        _cache[key] = (ceiling, bound)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return bound


def evidence_items(ref: str) -> str:
    """``ref``'s evidence array expanded as ``ev`` (invalid JSON or a
    non-array counts as empty)."""
    return (
        f"json_each(CASE WHEN json_valid({ref}.evidence) "
        f"THEN CASE WHEN json_type({ref}.evidence)='array' "
        f"THEN {ref}.evidence ELSE '[]' END ELSE '[]' END) ev"
    )


def evidence_source_exists(ref: str, condition: str) -> str:
    """``ref`` has at least one OBJECT evidence item satisfying ``condition``
    (written on ``ev``)."""
    return (
        f"EXISTS (SELECT 1 FROM {evidence_items(ref)} "
        f"WHERE ev.type='object' AND {condition})"
    )


def evidence_support_sql(ref: str, bound: BoundIds, *, authoritative: bool) -> str:
    """The ONLY text of the support predicate (PostgreSQL twin's docstring is
    the definition): ``ref`` has an evidence item whose source is in
    ``bound``.  Binds ``bound.param`` once.  Both branches use the
    non-driving ``member_of`` (see the module docstring)."""
    if authoritative:
        return evidence_source_exists(ref, member_of(EVIDENCE_ITEM_SOURCE, bound))
    return (
        "EXISTS (SELECT 1 FROM knowledge_object_sources kos "
        f"WHERE kos.object_id={ref}.id "
        f"AND kos.notebook_id={ref}.notebook_id "
        f"AND {member_of('kos.source_id', bound)})"
    )


def execute_with_ceiling(db: Any, sql: str, params: Sequence[Any]) -> Any:
    """Execute a statement that binds a ceiling (a plain execute on SQLite;
    see the module docstring)."""
    return db.execute(sql, tuple(params))
