"""A deep copy and a ``.snbk`` import keep every source's element order -- the
checks both backends run (``tests/test_copy_element_order.py`` and
``tests/postgres/test_copy_element_order_pg.py``).

Every reader orders a source's elements by ``created_at, <digits>, id``
(``app.repositories.element_reading_order``). A copy keeps ``created_at``; it
used to give every non-knowhow element a random ``el-<uuid>``, whose digits
term is 0, so the copy read in random order. The world here holds every id
form a copied source can carry:

* ``BIG``: 10 020 parsed elements (``el-<source>-NNNN``) with images
  backfilled after elements 12, 9 999 and 10 001 (``-g001``) -- the digit
  widths 4 and 5 and the backfill suffix;
* ``MIXED``: one source written at three instants -- parsed ids with two
  images on one anchor, promotion-style ``el-promo-<hash>`` and
  copy-of-a-copy ``el-<hex>`` ids (digits 0) sharing an instant with parsed
  ids (digits 4), and parsed ids again at a later instant;
* ``LEGACY``: a source that an earlier copy already scrambled (``el-<hex>``
  ids, one with a backfilled image) -- copying it again must keep the order it
  reads in now;
* the knowhow tables of the copy characterization world, whose element ids are
  pinned by the projection's stable formula (``el-kh-<hash(row, column)>``).

Each element's text is ``t:<its original id>``, so a copy's element names the
original it came from. Each reader of the original and of the copy must return
the same originals in the same order; chunks, knowledge evidence, source facts
and element vectors of the copy must name the same originals. Every reader is
reached through ``repo._runtime.<component>``.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from tests import copy_memory_cases as memory_cases
from tests import notebook_archive_cases as archive_cases
from tests import notebook_copy_characterization_cases as char_cases
from tests import notebook_import_cases as import_cases
from tests import source_element_reading_order_cases as order_cases
from tests.copy_memory_cases import Row

NB = "nb-copy-order"
OWNER = memory_cases.OWNER
BIG = "src-co-big"
BIG_COUNT = 10_020
BIG_IMAGES = (12, 9_999, 10_001)
MIXED = "src-co-mixed"
LEGACY = "src-co-legacy"
INSTANTS = order_cases.INSTANTS
OBJECT = "ko-co-1"
#: Chunk sizes: BIG is chunked 50 elements at a time, MIXED 7 at a time.
_CHUNK = {BIG: 50, MIXED: 7}


def _digest(seed: str, length: int = 32) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _element(source_id: str, element_id: str, element_type: str, at: str) -> Row:
    return Row("source_elements", {
        "id": element_id, "source_id": source_id, "element_type": element_type,
        "location_label": f"loc {element_id}", "text": f"t:{element_id}",
        "metadata": {"section_path": source_id}, "created_at": at,
    })


def _big() -> list[Row]:
    rows = []
    for index in range(1, BIG_COUNT + 1):
        rows.append(_element(BIG, f"el-{BIG}-{index:04d}", order_cases._type(index),
                             INSTANTS[0]))
        if index in BIG_IMAGES:
            rows.append(_element(BIG, f"el-{BIG}-{index:04d}-g001", "image", INSTANTS[0]))
    return rows


def _mixed() -> list[Row]:
    rows = []
    for index in range(1, 31):
        rows.append(_element(MIXED, f"el-{MIXED}-{index:04d}", order_cases._type(index),
                             INSTANTS[0]))
        if index == 12:
            rows += [_element(MIXED, f"el-{MIXED}-0012-g{s:03d}", "image", INSTANTS[0])
                     for s in (1, 2)]
    # One instant holding both digit groups: parsed ids (digits 4) and ids of
    # other forms (digits 0) -- the promotion and copy-of-a-copy shapes.
    for index in range(40, 46):
        rows.append(_element(MIXED, f"el-{MIXED}-{index:04d}", "paragraph", INSTANTS[1]))
    for index in range(10):
        rows.append(_element(MIXED, f"el-promo-{_digest(f'promo{index}')}", "paragraph",
                             INSTANTS[1]))
        rows.append(_element(MIXED, f"el-{_digest(f'copy{index}')}", "formula", INSTANTS[1]))
    for index in range(50, 56):
        rows.append(_element(MIXED, f"el-{MIXED}-{index:04d}", "heading", INSTANTS[2]))
    return rows


def _legacy() -> list[Row]:
    rows = []
    for index in range(25):
        element_id = f"el-{_digest(f'legacy{index}')}"
        rows.append(_element(LEGACY, element_id, order_cases._type(index + 1), INSTANTS[0]))
        if index == 3:
            rows.append(_element(LEGACY, f"{element_id}-g001", "image", INSTANTS[0]))
    return rows


def _source(source_id: str) -> Row:
    return Row("sources", {
        "id": source_id, "notebook_id": NB, "title": f"title {source_id}",
        "source_type": "markdown", "status": "extracted", "parse_status": "parsed",
        "created_at": INSTANTS[0], "updated_at": INSTANTS[0],
    })


def _reading_order(rows: list[Row], source_id: str) -> list[str]:
    from app.repositories.element_reading_order import reading_order_key

    elements = [row.values for row in rows
                if row.table == "source_elements" and row.values["source_id"] == source_id]
    return [e["id"] for e in sorted(
        elements, key=lambda e: (lambda k: (k[0], k[1], k[2].encode()))(
            reading_order_key(source_id, e["created_at"], e["id"])))]


def world() -> list[Row]:
    """The notebook: three ordinary sources, the knowhow tables, chunks over
    the ordinary sources in reading order, one knowledge object whose evidence
    names an element of each, a source fact and element vectors."""
    rows: list[Row] = [char_cases._notebook(NB, OWNER)]
    elements = _big() + _mixed() + _legacy()
    rows += [_source(source_id) for source_id in (BIG, MIXED, LEGACY)]
    rows += elements
    for source_id, size in _CHUNK.items():
        ordered = _reading_order(elements, source_id)
        for start in range(0, len(ordered), size):
            chunk_id = f"chunk-co-{source_id}-{start}"
            rows.append(Row("chunks", {
                "id": chunk_id, "notebook_id": NB, "source_id": source_id,
                "text": f"chunk {source_id} {start}", "section_path": source_id,
                "element_ids": ordered[start:start + size], "created_at": INSTANTS[0],
            }))
    cited = [(BIG, f"el-{BIG}-10001-g001"), (MIXED, f"el-promo-{_digest('promo3')}"),
             (LEGACY, f"el-{_digest('legacy3')}-g001")]
    rows.append(Row("knowledge_objects", {
        "id": OBJECT, "notebook_id": NB, "object_type": "concept", "status": "approved",
        "source_id": BIG, "payload": {"name": "copied order"},
        "evidence": [memory_cases.evidence(s, e, memory=False) for s, e in cited],
        "created_at": INSTANTS[0], "updated_at": INSTANTS[0],
    }))
    rows += memory_cases._facts(NB, MIXED, "co", OBJECT, f"el-{MIXED}-0012-g002", memory=False)
    for row in _mixed():
        rows.append(Row("element_embeddings", {
            "element_id": row.values["id"], "source_id": MIXED, "notebook_id": NB,
            "vector": f"v:{row.values['id']}".encode(), "created_at": INSTANTS[0],
        }))
    rows += char_cases._knowhow_rows(NB, chunk_without_elements=False)
    return import_cases.resized(rows)


def seed(repo, backend: archive_cases.Backend) -> None:
    with repo._runtime.database.write() as db:
        if backend.name == "postgres":
            db.execute("SET LOCAL statement_timeout = '0'")
        for row in world():
            backend.insert(db, row.table, dict(row.values))


# ------------------------------------------------------------- the runs
def deep_copy(repo) -> str:
    return repo._runtime.sharing.copy_notebook(NB, new_owner_id=OWNER).id


def export_and_import(repo, tmp: Path, monkeypatch) -> str:
    archive = tmp / "copy-order.snbk"
    archive_cases.export(repo, NB, archive)
    import_cases.target_models(repo, monkeypatch)
    return import_cases.run_import(repo, archive).notebook_id


# ------------------------------------------------------------- comparing
def _rows(repo, sql: str, params: tuple) -> list[dict]:
    if not repo.settings.database_url.startswith("sqlite"):
        sql = sql.replace("?", "%s")
    with repo._runtime.database.connect() as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _sources(repo, notebook_id: str) -> dict[str, str]:
    """title -> source id."""
    return {row["title"]: row["id"] for row in _rows(
        repo, "SELECT id, title FROM sources WHERE notebook_id = ?", (notebook_id,))}


def _origins(repo, notebook_id: str) -> dict[str, str]:
    """element id -> the original element id it was copied from (its text)."""
    rows = _rows(repo, "SELECT e.id, e.text FROM source_elements e JOIN sources s "
                       "ON s.id = e.source_id WHERE s.notebook_id = ?", (notebook_id,))
    return {row["id"]: row["text"] for row in rows}


def _readers(source_id: str, count: int) -> dict[str, Callable]:
    r = order_cases
    return {
        "whole": lambda repo: r.read_whole(repo, source_id),
        "walk": lambda repo: r.read_walk(repo, source_id, 997),
        "walk-small": lambda repo: r.read_walk(repo, source_id, 7),
        **{f"window-{offset}": (lambda o: lambda repo: r.read_window(repo, source_id, o, 40))(offset)
           for offset in (0, 990, 9_960, 10_000, max(0, count - 3))},
        **{f"typed-{t}": (lambda t: lambda repo: r.read_typed(repo, source_id, t, 50))(t)
           for t in ("formula", "heading", "image", "paragraph")},
        "chunking": lambda repo: r.read_chunking(repo, source_id),
        "headings": lambda repo: r.read_headings(repo, source_id),
        "preview": lambda repo: r.read_preview(repo, source_id, count),
        "backfill": lambda repo: r.read_backfill_plan(repo, source_id),
    }


def check_order_kept(repo, copy_id: str) -> None:
    """Every reader returns the copy's elements in the original's order, the
    keyset anchors land on the same page, and every reference of the copy names
    the element it named in the original."""
    copied = _sources(repo, copy_id)
    origin = _origins(repo, copy_id)

    def as_original(ids):
        return [origin[i][2:] for i in ids]  # a copy's text is "t:<original id>"

    by_origin = {text[2:]: element_id for element_id, text in origin.items()}
    for source_id in (BIG, MIXED, LEGACY):
        twin = copied[f"title {source_id}"]
        expected = order_cases.read_whole(repo, source_id)
        if source_id == BIG:
            assert expected == order_cases.numeric_order(BIG, BIG_COUNT, [(i, 1) for i in BIG_IMAGES])
        for name, read in _readers(source_id, len(expected)).items():
            want = read(repo)
            got = _twin_reader(name, twin, len(expected))(repo)
            assert as_original(got) == want, (source_id, name)
        for anchor in (expected[0], expected[len(expected) // 2], expected[-1]):
            want_offset, want_page = order_cases.read_anchor(repo, source_id, anchor, 40)
            got_offset, got_page = order_cases.read_anchor(repo, twin, by_origin[anchor], 40)
            assert (got_offset, as_original(got_page)) == (want_offset, want_page), anchor
    probe = [f"el-{MIXED}-0045", f"el-{BIG}-1135", f"el-promo-{_digest('promo1')}",
             f"el-{BIG}-10003", f"el-{_digest('copy2')}", f"el-{_digest('legacy7')}"]
    want = order_cases.read_step_ordinals(repo, probe)
    got = order_cases.read_step_ordinals(repo, [by_origin[i] for i in probe])
    assert {origin[i][2:]: rank for i, rank in got.items()} == want
    _check_knowhow(repo, copied)
    _check_references(repo, copy_id, origin)
    # The operations runbook's query for copies made before the fix: it finds
    # the scrambled source of the world, and never a source this copy made.
    found = {(row["notebook_id"], row["source_id"]) for row in _rows(repo, SCRAMBLED_SOURCES_SQL, ())}
    assert (NB, LEGACY) in found
    assert not {nb for nb, _source in found if nb == copy_id}


#: Verbatim in ``docs/operations.md`` and ``docs/operations_zh.md`` (pinned by
#: ``test_runbook_quotes_the_scrambled_sources_query``).
SCRAMBLED_SOURCES_SQL = """SELECT s.notebook_id, e.source_id, COUNT(*) AS minted_elements
FROM source_elements e JOIN sources s ON s.id = e.source_id
WHERE s.source_type NOT IN ('knowhow', 'promotion', 'memory')
  AND substr(e.id,1,length(e.source_id)+4) <> ('el-'||e.source_id||'-')
GROUP BY s.notebook_id, e.source_id
HAVING COUNT(*) > 1;"""


def _twin_reader(name: str, source_id: str, count: int) -> Callable:
    return _readers(source_id, count)[name]


def _check_knowhow(repo, copied: dict) -> None:
    """A knowhow cell's element is the projection's, not the copy's: its id is
    the projection's formula of the (new) row and column, and the run schedules
    a reprojection of every copied table after publish, which rewrites the
    cells' elements -- their text and instant included -- in the background.
    So the copy's part is the same number of cells, each under the formula id
    of the row and column it names."""
    from app.services.knowhow.ids import element_id as knowhow_element_id

    for _table, hidden, title, _columns, _cells in char_cases.KNOWHOW:
        elements = repo._runtime.source_store.source_elements(copied[title])
        assert len(elements) == len(repo._runtime.source_store.source_elements(hidden)) > 0
        for element in elements:
            meta = element.metadata["knowhow"]
            assert element.id == knowhow_element_id(meta["row_id"], meta["column_id"])


def _check_references(repo, copy_id: str, origin: dict) -> None:
    def names(ids):
        return [origin[i][2:] for i in ids]

    def chunk_rows(notebook_id):
        return {row["text"]: _json(row["element_ids"]) for row in _rows(
            repo, "SELECT text, element_ids FROM chunks WHERE notebook_id = ? AND text LIKE ?",
            (notebook_id, "chunk %"))}

    chunks, originals = chunk_rows(copy_id), chunk_rows(NB)
    assert chunks.keys() == originals.keys() and chunks
    for text, element_ids in chunks.items():
        assert names(element_ids) == originals[text], text
    (evidence,) = [_json(row["evidence"]) for row in _rows(
        repo, "SELECT evidence FROM knowledge_objects WHERE notebook_id = ? AND object_type = 'concept'",
        (copy_id,))]
    want = [entry["element_id"] for entry in _json(_rows(
        repo, "SELECT evidence FROM knowledge_objects WHERE id = ?", (OBJECT,))[0]["evidence"])]
    assert names([entry["element_id"] for entry in evidence]) == want
    (fact,) = _rows(repo, "SELECT element_id FROM knowledge_source_fact_elements "
                          "WHERE notebook_id = ?", (copy_id,))
    assert names([fact["element_id"]]) == [f"el-{MIXED}-0012-g002"]
    vectors = _rows(repo, "SELECT element_id, vector FROM element_embeddings WHERE notebook_id = ?",
                    (copy_id,))
    assert len(vectors) == len(_mixed())
    for row in vectors:
        expected = (f"v:{origin[row['element_id']][2:]}".encode() * 16)[:16]
        assert bytes(row["vector"]) == expected, row["element_id"]
