"""A source's elements in reading order, past 9 999 elements -- the checks both
backends run (``tests/test_source_element_reading_order.py`` and
``tests/postgres/test_source_element_reading_order_pg.py``).

Parsed elements are minted ``el-<source_id>-NNNN`` (four-digit zero padding)
and image backfill appends ``-gMMM`` to its anchor. Byte order of those ids
stops being the document order at the 10 000th element (``...-10000`` sorts
between ``...-1000`` and ``...-1001``); every reader now orders by
``app.repositories.element_reading_order``. Two worlds:

* ``BIG``: a 10 020-element source (with images backfilled after elements
  12, 9 999 and 10 001) and a 10 001-element source whose only non-blank
  elements are 1 001 and 10 000. Every reader must return the numeric order.
* ``SMALL``: sources the change must NOT move -- 9 999 parsed elements with
  backfilled images, knowhow-cell ids (``el-kh-<hash>``) and promotion ids
  (``el-promo-<hash>``) written at three different instants. Every reader
  must return exactly what the old ``(created_at, id)`` / ``id`` ordering
  returned, computed here from the raw rows.

Each reader is reached through ``repo._runtime.<component>``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Sequence

from app.domain.source_positions import page_at_positions

NOTEBOOK = "nb-reading-order"
BIG = "src-ro-big"
BIG_COUNT = 10_020
BIG_IMAGES = (12, 9_999, 10_001)
EXCERPT = "src-ro-excerpt"
EXCERPT_COUNT = 10_001
EXCERPT_TEXT = {1_001: "first words", 10_000: "later words"}
SMALL = "src-ro-small"
SMALL_COUNT = 9_999
SMALL_IMAGES = ((12, 1), (12, 2), (9_999, 1))
KNOWHOW = "src-ro-kh"
PROMOTION = "src-ro-promo"
INSTANTS = (
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T00:00:01+00:00",
    "2026-01-01T00:00:02+00:00",
    "2026-01-01T00:00:03+00:00",
)


@dataclass(frozen=True)
class Backend:
    """How one backend spells a placeholder and a timestamp."""

    name: str
    placeholder: str
    timestamp: Callable[[str], str]
    executemany: Callable[[object, str, list], None]


def _type(index: int) -> str:
    if index % 50 == 0:
        return "heading"
    if index % 7 == 0:
        return "formula"
    return "paragraph"


def _parsed_rows(source_id: str, count: int, images, text_for,
                 instant: str = INSTANTS[0]) -> list[tuple]:
    rows = []
    anchors: dict[int, list[int]] = {}
    for anchor, suffix in images:
        anchors.setdefault(anchor, []).append(suffix)
    for index in range(1, count + 1):
        rows.append((f"el-{source_id}-{index:04d}", source_id, _type(index),
                     f"p{index}", text_for(index),
                     json.dumps({"section_path": f"S{index // 50}"}), instant))
        for suffix in anchors.get(index, ()):
            rows.append((f"el-{source_id}-{index:04d}-g{suffix:03d}", source_id,
                         "image", "img", f"image after {index}", "{}", instant))
    return rows


def _hashed_rows(source_id: str, prefix: str, count: int) -> list[tuple]:
    rows = []
    for index in range(count):
        digest = hashlib.sha256(f"{source_id}:{index}".encode()).hexdigest()[:32]
        rows.append((f"{prefix}{digest}", source_id,
                     "formula" if index % 3 == 0 else "paragraph", f"r{index}",
                     f"cell {index}", "{}", INSTANTS[1 + index % 3]))
    return rows


def numeric_order(source_id: str, count: int, images) -> list[str]:
    """The document order of a parsed source: index, then its images."""
    order = []
    anchors: dict[int, list[int]] = {}
    for anchor, suffix in images:
        anchors.setdefault(anchor, []).append(suffix)
    for index in range(1, count + 1):
        order.append(f"el-{source_id}-{index:04d}")
        order.extend(f"el-{source_id}-{index:04d}-g{s:03d}" for s in sorted(anchors.get(index, ())))
    return order


def seed(repo, backend: Backend, world: str) -> dict[str, list[tuple]]:
    """Write one world; returns the raw rows per source."""
    p = backend.placeholder
    if world == "big":
        sources = {
            BIG: _parsed_rows(BIG, BIG_COUNT, [(i, 1) for i in BIG_IMAGES],
                              lambda i: f"text {i}"),
            # Its own instant, so the notebook-wide step order reads BIG whole
            # first (a source's elements share its parse instant).
            EXCERPT: _parsed_rows(EXCERPT, EXCERPT_COUNT, (),
                                  lambda i: EXCERPT_TEXT.get(i, "  "), INSTANTS[1]),
        }
    else:
        sources = {
            SMALL: _parsed_rows(SMALL, SMALL_COUNT, SMALL_IMAGES, lambda i: f"text {i}"),
            KNOWHOW: _hashed_rows(KNOWHOW, "el-kh-", 60),
            PROMOTION: _hashed_rows(PROMOTION, "el-promo-", 45),
        }
    with repo._runtime.database.write() as db:
        if backend.name == "postgres":
            db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
            f"created_at,updated_at) VALUES ({p},{p},'','S','draft','user-local',{p},{p})",
            (NOTEBOOK + "-" + world, world, backend.timestamp(INSTANTS[0]),
             backend.timestamp(INSTANTS[0])),
        )
        for source_id in sources:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,file_name,file_path,file_size,created_at,updated_at) "
                f"VALUES ({p},{p},{p},'markdown','extracted','parsed','x.md','',1,{p},{p})",
                (source_id, NOTEBOOK + "-" + world, source_id,
                 backend.timestamp(INSTANTS[0]), backend.timestamp(INSTANTS[0])),
            )
        backend.executemany(
            db,
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            f"text,metadata,created_at) VALUES ({p},{p},{p},{p},{p},{p},{p})",
            [row[:6] + (backend.timestamp(row[6]),)
             for rows in sources.values() for row in rows],
        )
    return sources


def legacy_order(rows: Sequence[tuple], *, created_first: bool) -> list[str]:
    """What the readers returned before: byte order of ``(created_at, id)``
    (or of ``id`` alone). Every instant here is the same-width ISO text, so
    text order is time order on both backends."""
    key = (lambda row: (row[6], row[0].encode())) if created_first else (
        lambda row: row[0].encode())
    return [row[0] for row in sorted(rows, key=key)]


# --------------------------------------------------------------- readers
def _ids(items) -> list[str]:
    return [item.id if hasattr(item, "id") else item["id"] for item in items]


def read_whole(repo, source_id: str) -> list[str]:
    return _ids(repo._runtime.source_store.source_elements(source_id))


# A cursor that stops advancing must fail the check, not hang it: no world
# here holds more than this many elements.
_MAX_WALKED = 25_000


def read_walk(repo, source_id: str, limit: int) -> list[str]:
    store, after, out = repo._runtime.source_store, None, []
    while len(out) <= _MAX_WALKED:
        items, after = store.source_elements_after(source_id, after, limit)
        out.extend(_ids(items))
        if after is None:
            return out
    raise AssertionError(f"keyset walk of {source_id} did not terminate")


def read_window(repo, source_id: str, offset: int, limit: int) -> list[str]:
    page = repo._runtime.source_store.source_elements_page(
        source_id, offset=offset, limit=limit)
    return _ids(page.items)


def read_anchor(repo, source_id: str, anchor: str, limit: int) -> tuple[int, list[str]]:
    page = repo._runtime.source_store.source_elements_page(
        source_id, limit=limit, anchor_element_id=anchor)
    return page.offset, _ids(page.items)


def read_positions(repo, source_id: str, positions: Sequence[int]):
    return repo._runtime.source_store.source_elements_page(source_id, positions=positions)


def read_typed(repo, source_id: str, element_type: str, limit: int) -> list[str]:
    store, after, out = repo._runtime.source_store, None, []
    with repo._runtime.database.connect() as db:
        while len(out) <= _MAX_WALKED:
            rows = store.element_page_rows(db, source_id, element_type, after, limit)
            out.extend(_ids(rows))
            if len(rows) < limit:
                return out
            after = (rows[-1]["created_at"], rows[-1]["id"])
    raise AssertionError(f"typed walk of {source_id} did not terminate")


def read_chunking(repo, source_id: str) -> list[str]:
    return _ids(repo._runtime.chunk_store.source_elements_for_chunking(source_id))


def read_headings(repo, source_id: str) -> list[str]:
    return _ids(repo._runtime.chunk_store.source_heading_elements(source_id))


def read_preview(repo, source_id: str, limit: int) -> list[str]:
    rows, _clipped = repo._runtime.command_catalog.catalog.preview_elements(
        source_id, limit=limit, text_chars=40)
    return _ids(rows)


def read_backfill_plan(repo, source_id: str) -> list[str]:
    return _ids(repo.maintenance.image_backfill_source_state(source_id)["elements"])


def read_step_ordinals(repo, element_ids: Sequence[str],
                       owner_notebook_id: str | None = None) -> dict[str, int]:
    with repo._runtime.database.connect() as db:
        _texts, ordinal = repo._runtime.knowledge._element_texts(
            db, list(element_ids), with_ordinal=True,
            owner_notebook_id=owner_notebook_id)
    return ordinal


def read_excerpt(repo, notebook_id: str, source_id: str) -> str:
    snapshot = repo._runtime.source_store.question_suggestion_snapshot(
        notebook_id, source_limit=10, text_chars=40)
    (row,) = [row for row in snapshot["sources"] if row["id"] == source_id]
    return row["excerpt"]


def _typed(order: Sequence[str], rows: Sequence[tuple], element_type: str) -> list[str]:
    types = {row[0]: row[2] for row in rows}
    return [element_id for element_id in order if types[element_id] == element_type]


# ------------------------------------------------------------ the checks
def check_big_source_reads_in_numeric_order(repo, rows: dict[str, list[tuple]]) -> None:
    expected = numeric_order(BIG, BIG_COUNT, [(i, 1) for i in BIG_IMAGES])
    big_rows = rows[BIG]
    assert len(expected) == len(big_rows) == BIG_COUNT + len(BIG_IMAGES)
    # The defect itself, stated once: the old order is not the document order.
    assert legacy_order(big_rows, created_first=True) != expected
    assert read_whole(repo, BIG) == expected
    assert read_walk(repo, BIG, 997) == expected
    assert read_walk(repo, BIG, 7)[9_990:10_020] == expected[9_990:10_020]
    # 990: a window read directly (near the head) that crosses ...-1000.
    for offset in (0, 990, 9_960, 10_000, len(expected) - 3):
        assert read_window(repo, BIG, offset, 40) == expected[offset:offset + 40], offset
    for anchor in (f"el-{BIG}-1134", f"el-{BIG}-10001-g001", f"el-{BIG}-10020"):
        position = expected.index(anchor)
        offset, page = read_anchor(repo, BIG, anchor, 40)
        assert offset == (position // 40) * 40, anchor
        assert page == expected[offset:offset + 40], anchor
    elements = {element.id: element for element in
                repo._runtime.source_store.source_elements(BIG)}
    ordered = [elements[element_id] for element_id in expected]
    for positions in ([0, 9_998, 10_000, 10_001, len(expected) - 1],
                      list(range(9_950, 10_050)),
                      [len(expected) - 1]):
        assert read_positions(repo, BIG, positions) == page_at_positions(
            ordered, positions), positions[:3]
    for element_type in ("formula", "heading", "image"):
        assert read_typed(repo, BIG, element_type, 50) == _typed(
            expected, big_rows, element_type), element_type
    assert read_chunking(repo, BIG) == expected
    assert read_headings(repo, BIG) == _typed(expected, big_rows, "heading")
    assert read_preview(repo, BIG, len(expected)) == expected
    assert read_preview(repo, BIG, 10_005) == expected[:10_005]
    assert read_backfill_plan(repo, BIG) == expected
    # Procedure steps sort by the notebook-wide document order: BIG (its
    # instant first) in reading order, then EXCERPT.
    probe = [f"el-{BIG}-1135", f"el-{BIG}-10003", f"el-{BIG}-0002"]
    excerpt_order = numeric_order(EXCERPT, EXCERPT_COUNT, ())
    notebook_order = {element_id: index for index, element_id in
                      enumerate(expected + excerpt_order)}
    assert read_step_ordinals(repo, probe) == notebook_order
    # The owner-scoped spelling (a promoted procedure's steps read inside
    # their own library) orders the same way.
    assert read_step_ordinals(repo, probe, NOTEBOOK + "-big") == notebook_order


def check_first_excerpt_is_the_first_nonblank_element(repo) -> None:
    # Old byte order put ...-10000 (non-blank) before ...-1001; the excerpt is
    # the document's first non-blank element.
    assert read_excerpt(repo, NOTEBOOK + "-big", EXCERPT) == EXCERPT_TEXT[1_001]


TAIL = "src-ro-tail"


def check_continuation_crosses_digits_and_instants(repo, backend: Backend) -> None:
    """A keyset page that starts in the first range and has to finish in the
    other two (more digits at the cursor's instant, then a later instant):
    the store's own walk, at every page size."""
    p = backend.placeholder
    rows = [(f"el-{TAIL}-{i:04d}", INSTANTS[0]) for i in range(1, 6)]
    rows += [(f"el-{TAIL}-{i}", INSTANTS[0]) for i in (10_000, 10_001)]
    # A later instant whose id sorts before every earlier one byte-wise.
    rows += [(f"el-{TAIL}-0000", INSTANTS[1])]
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
            f"created_at,updated_at) VALUES ({p},'tail','','S','draft','user-local',{p},{p})",
            (NOTEBOOK + "-tail", backend.timestamp(INSTANTS[0]), backend.timestamp(INSTANTS[0])),
        )
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,created_at,updated_at) VALUES "
            f"({p},{p},'tail','markdown','extracted','parsed','x.md','',1,{p},{p})",
            (TAIL, NOTEBOOK + "-tail", backend.timestamp(INSTANTS[0]),
             backend.timestamp(INSTANTS[0])),
        )
        backend.executemany(
            db,
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            f"text,metadata,created_at) VALUES ({p},{p},'paragraph','p','t','{{}}',{p})",
            [(element_id, TAIL, backend.timestamp(at)) for element_id, at in rows],
        )
    expected = [element_id for element_id, _at in rows]
    for limit in range(1, len(expected) + 2):
        assert read_walk(repo, TAIL, limit) == expected, limit
        assert read_typed(repo, TAIL, "paragraph", limit) == expected, limit


def check_small_sources_read_exactly_as_before(repo, rows: dict[str, list[tuple]]) -> None:
    for source_id, source_rows in rows.items():
        by_time = legacy_order(source_rows, created_first=True)
        by_id = legacy_order(source_rows, created_first=False)
        if source_id == SMALL:
            # a parsed source below the boundary: old order IS document order
            assert by_time == by_id == numeric_order(SMALL, SMALL_COUNT, SMALL_IMAGES)
        else:
            assert by_time != by_id  # three instants: the two old keys differ
        assert read_whole(repo, source_id) == by_time, source_id
        assert read_walk(repo, source_id, 37) == by_time, source_id
        for offset in (0, 40, max(0, len(by_time) - 41)):
            assert read_window(repo, source_id, offset, 40) == by_time[offset:offset + 40]
        for anchor in (by_time[0], by_time[len(by_time) // 2], by_time[-1]):
            position = by_time.index(anchor)
            offset, page = read_anchor(repo, source_id, anchor, 40)
            assert (offset, page) == ((position // 40) * 40,
                                      by_time[(position // 40) * 40:(position // 40) * 40 + 40])
        elements = {element.id: element for element in
                    repo._runtime.source_store.source_elements(source_id)}
        ordered = [elements[element_id] for element_id in by_time]
        for positions in ([0, 3, len(by_time) - 1],
                          list(range(0, len(by_time), max(1, len(by_time) // 90)))[:100]):
            assert read_positions(repo, source_id, positions) == page_at_positions(
                ordered, positions)
        for element_type in ("formula", "heading", "image", "paragraph"):
            assert read_typed(repo, source_id, element_type, 25) == _typed(
                by_time, source_rows, element_type), (source_id, element_type)
        assert read_chunking(repo, source_id) == by_id, source_id
        assert read_headings(repo, source_id) == _typed(by_id, source_rows, "heading")
        assert read_preview(repo, source_id, 500) == by_id[:500], source_id
        assert read_backfill_plan(repo, source_id) == by_id, source_id
    notebook_rows = [row for source_rows in rows.values() for row in source_rows]
    notebook_order = legacy_order(notebook_rows, created_first=True)
    probe = [rows[KNOWHOW][5][0], rows[SMALL][100][0], rows[PROMOTION][7][0]]
    ordinal = read_step_ordinals(repo, probe)
    assert ordinal == {element_id: index for index, element_id in enumerate(notebook_order)}
