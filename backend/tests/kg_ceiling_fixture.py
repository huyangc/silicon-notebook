"""Shared fixture + reference model for the KG enumeration source ceiling
(PR-B·B1): ``knowledge_object_page_rows(allowed_source_ids=...)`` and
``count_knowledge(supported_by_source_ids=..., excluding_owner_source_ids=...)``.

One spec seeds BOTH backends (the SQLite store test and the PostgreSQL twin),
and one pure-Python reference says what either must return, so "PG and SQLite
agree" is checked against an independent statement of the semantics rather
than only against each other.

Support = at least one EVIDENCE item (``evidence[].source_id``) in the
ceiling.  The object's own ``source_id`` column is its OWNER — the source it
was extracted from — and is what ``excluding_owner_source_ids`` reads.  The
spec deliberately contains objects where the two disagree (``ko-03``,
``ko-09``) so reading the wrong column changes the answer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

NOTEBOOK_ID = "nb-ceil"
OTHER_NOTEBOOK_ID = "nb-ceil-other"
OBJECT_TYPE = "concept"
USABLE = ("approved", "draft")
SOURCES = ("s-in1", "s-in2", "s-out1", "s-out2", "s-mem")
INCLUDE_CEILING = ("s-in1", "s-in2")
ODD_SOURCE = "s-odd\x1fid"


@dataclass(frozen=True)
class Raw:
    """An evidence element written verbatim (a JSON string, number, or
    hand-built object) instead of being expanded into an evidence object."""
    value: object


@dataclass(frozen=True)
class SpecObject:
    id: str
    evidence: tuple            # items: a source id (str → evidence object) or Raw
    owner: str = ""
    status: str = "approved"
    object_type: str = OBJECT_TYPE
    notebook_id: str = NOTEBOOK_ID
    second: int = 0            # created_at offset; ties exercise the id half


def _objects() -> tuple[SpecObject, ...]:
    rows = [
        SpecObject("ko-01", ("s-in1",), owner="s-in1", second=1),
        SpecObject("ko-02", ("s-out1",), owner="s-out1", second=2),
        # Two evidence sources, one inside one outside: supported.
        SpecObject("ko-03", ("s-out1", "s-in2"), owner="s-out1", second=3),
        # No evidence at all: listed without a ceiling, never under one.
        SpecObject("ko-04", (), owner="", second=4),
        # The ceiling excludes EVERY evidence item.
        SpecObject("ko-05", ("s-out2", "s-out1"), owner="s-out2", second=5),
        # Raw page returns it (status is the caller's filter); count drops it.
        SpecObject("ko-06", ("s-in2",), owner="s-in2", status="deprecated", second=6),
        # Owned by a private Memory source, evidence inside the ceiling.
        SpecObject("ko-07", ("s-in1",), owner="s-mem", second=7),
        # Name no source: an object with an empty source id, a JSON STRING
        # element (not an object), a JSON number.
        SpecObject("ko-08", (Raw({"source_id": ""}), Raw("s-in1"), Raw(7)), owner="",
                   second=8),
        # OWNER inside the ceiling, evidence outside: NOT supported.
        SpecObject("ko-09", ("s-out1",), owner="s-in1", second=9),
    ]
    # A long out-of-ceiling stretch: a predicate applied after the LIMIT
    # starves every page that lands in it.
    rows.extend(
        SpecObject(f"ko-1{index}", ("s-out2",), owner="s-out2", second=10 + index)
        for index in range(10)
    )
    rows.extend([
        # Same created_at: the keyset's id tie-break decides the order.
        SpecObject("ko-20", ("s-in1",), owner="s-in1", status="draft", second=30),
        SpecObject("ko-21", ("s-in2",), owner="s-in2", second=30),
        # Other type / other notebook with in-ceiling evidence: never listed.
        SpecObject("ko-30", ("s-in1",), owner="s-in1", object_type="claim", second=31),
        SpecObject("ko-31", ("s-in1",), owner="s-in1", notebook_id=OTHER_NOTEBOOK_ID,
                   second=32),
        # A source id containing the PostgreSQL bind form's separator (0x1F):
        # only an exact match may admit it, never a split.
        SpecObject("ko-22", (ODD_SOURCE,), owner=ODD_SOURCE, second=33),
    ])
    return tuple(rows)


OBJECTS = _objects()


def evidence_json(obj: SpecObject) -> str:
    items = []
    for item in obj.evidence:
        if isinstance(item, Raw):
            items.append(item.value)
        else:
            items.append({"source_id": item, "source_title": item,
                          "element_id": f"el-{obj.id}-{item}",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "q", "confidence": 1.0})
    return json.dumps(items)


def created_at(obj: SpecObject) -> str:
    return f"2026-09-01T00:00:{obj.second:02d}+00:00"


def evidence_sources(obj: SpecObject) -> set[str]:
    return {item for item in obj.evidence if isinstance(item, str) and item}


def seed(
    execute: Callable[[str, tuple], object], mark: str, *, backfilled: bool,
    flatten: Callable[[str], set],
) -> None:
    """Insert the spec through ``execute(sql, params)``; ``mark`` is the
    dialect's placeholder (``?`` / ``%s``).  ``backfilled`` certifies the
    reverse index; uncertified leaves the index EMPTY, so only the
    authoritative evidence-JSON branch can produce the expected answer.

    The certified index rows are built by ``flatten`` — the backend's
    PRODUCTION ``KnowledgeStore.source_ids_from_evidence`` — and checked
    against this module's own ``evidence_sources`` first, so "reverse index =
    flattened evidence" is verified, not assumed."""
    def q(sql: str) -> str:
        return sql.replace("?", mark)

    now = "2026-09-01T00:00:00+00:00"
    for notebook_id in (NOTEBOOK_ID, OTHER_NOTEBOOK_ID):
        execute(q("INSERT INTO notebooks (id,name,created_at,updated_at) VALUES (?,?,?,?)"),
                (notebook_id, notebook_id, now, now))
        execute(q("INSERT INTO unified_kg_state (notebook_id,source_index_backfilled,"
                  "updated_at) VALUES (?,?,?)"),
                (notebook_id, 1 if backfilled else 0, now))
    for obj in OBJECTS:
        execute(q("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                  "payload,evidence,source_id,created_at,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)"),
                (obj.id, obj.notebook_id, obj.object_type, obj.status, "",
                 json.dumps({"name": obj.id}), evidence_json(obj), obj.owner,
                 created_at(obj), created_at(obj)))
        flattened = set(flatten(evidence_json(obj)))
        assert flattened == evidence_sources(obj), (obj.id, flattened)
        if backfilled:
            for source_id in sorted(flattened):
                execute(q("INSERT INTO knowledge_object_sources (object_id,source_id,"
                          "notebook_id) VALUES (?,?,?)"),
                        (obj.id, source_id, obj.notebook_id))


def _ceiling(ids: Optional[Sequence[str]]) -> Optional[set[str]]:
    return None if ids is None else {value for value in ids if value}


def reference_page_ids(allowed: Optional[Sequence[str]]) -> list[str]:
    """Every raw row the keyset walk must return, in order (any status)."""
    ceiling = _ceiling(allowed)
    rows = [o for o in OBJECTS
            if o.notebook_id == NOTEBOOK_ID and o.object_type == OBJECT_TYPE
            and (ceiling is None or evidence_sources(o) & ceiling)]
    return [o.id for o in sorted(rows, key=lambda o: (o.second, o.id))]


def reference_count(
    supported: Optional[Sequence[str]], excluding: Sequence[str] = ()
) -> int:
    ceiling = _ceiling(supported)
    excluded = {value for value in excluding if value}
    return sum(
        1 for o in OBJECTS
        if o.notebook_id == NOTEBOOK_ID and o.object_type == OBJECT_TYPE
        and o.status in USABLE and o.owner not in excluded
        and (ceiling is None or evidence_sources(o) & ceiling)
    )


# Past SQLite's default SQLITE_MAX_VARIABLE_NUMBER (32766): one placeholder per
# id would raise, so this proves the ceiling travels as ONE parameter.
HUGE_CEILING = tuple(f"s-absent-{index:05d}" for index in range(40_000)) + ("s-in1",)

CEILINGS: dict[str, Optional[tuple]] = {
    "none": None,
    "include": INCLUDE_CEILING,
    "deny_all": (),
    "blank_only": ("",),
    "outside_only": ("s-out1",),
    "duplicates_and_blank": ("s-in2", "", "s-in2"),
    # Separator collisions: a joined pair must not admit either half, and
    # the odd id must match itself exactly.
    "separator_joined": ("s-in1\x1fs-in2",),
    "separator_exact": (ODD_SOURCE, "s-in2"),
    "huge": HUGE_CEILING,
}

EXCLUSIONS: dict[str, tuple] = {
    "none": (),
    "memory": ("s-mem",),
    "memory_and_blank": ("s-mem", ""),
}


def walk_pages(page: Callable[[object, int], list], limit: int) -> list[list[str]]:
    """Drive a keyset walk to exhaustion; ``page(after, limit)`` returns rows."""
    pages: list[list[str]] = []
    after = None
    for _ in range(len(OBJECTS) + 2):
        rows = page(after, limit)
        if not rows:
            break
        pages.append([str(row["id"]) for row in rows])
        after = (rows[-1]["created_at"], str(rows[-1]["id"]))
        if len(rows) < limit:
            break
    return pages


class RecordingConnection:
    """Proxy that records every ``execute`` so a test can pin the exact
    statement and parameters (byte-identity for the ``None`` path, zero
    statements for the deny-all path)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, tuple]] = []
        self.options: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, sql, params=(), **options):
        self.calls.append((sql, tuple(params)))
        self.options.append(dict(options))
        return self._inner.execute(sql, params, **options)
