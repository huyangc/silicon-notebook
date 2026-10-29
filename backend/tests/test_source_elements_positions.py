"""SQLite ``source_elements_page(positions=)`` (debt fix F3).

* The adapter answers exactly what ``app.domain.source_positions
  .page_at_positions`` answers over the adapter's own ``source_elements`` --
  the contract the in-memory test doubles reuse.
* One positional read is ONE statement (one WAL snapshot for the count and
  the elements), and every position is answered by a term whose plan is the
  windowed read's own ``LIMIT 1 OFFSET`` plan.
* End to end over a real store, ``prepare_source_overview`` cites the promised
  positions of the stored order (spread: evenly spaced, ending on the last
  element; opening: the first ones) and issues 1 + ceil(k / 100) reads.
The PostgreSQL twin is ``tests/postgres/test_source_elements_positions_pg.py``.
"""
from __future__ import annotations

import json
import math

import pytest

from app.core.config import Settings
from app.domain.source_positions import MAX_POSITIONS_PER_READ, page_at_positions
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.source_store import READING_ORDER_SQL
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'positions.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed(repo, source_id: str, count: int) -> str:
    notebook = repo.create_notebook(NotebookCreate(name=f"nb-{source_id}"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook.id, source_id, "markdown", "parsed", "parsed",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        # Two created_at values and ids whose text order is not insertion
        # order, so "position" really is (created_at, id) order.
        db.executemany(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"el-{source_id}-{(index * 37) % count:05d}-{index}", source_id,
                 "paragraph", f"第{index + 1}页", f"原文 {index} " * (1 + index % 9),
                 json.dumps({"section_path": f"章{index // 10}"}) if index % 2 else "{}",
                 "2026-01-01T00:00:00" if index < count // 3 else "2026-01-02T00:00:00")
                for index in range(count)
            ],
        )
    return notebook.id


POSITION_CASES = [
    [], [0], [149], [150], [151, 0], [5, 5, 5], [149, 0, 75, 3],
    list(range(0, 150, 3)) + [149], list(range(100)),
    [10**9], [-4, 2],
]


@pytest.mark.parametrize("positions", POSITION_CASES)
def test_positions_answer_exactly_what_the_reference_answers(repo, positions):
    _seed(repo, "src-a", 150)
    _seed(repo, "src-b", 40)  # a neighbour the reads must never reach
    store = repo._runtime.source_store
    expected = page_at_positions(store.source_elements("src-a"), positions)
    assert store.source_elements_page("src-a", positions=positions) == expected


def test_refusals_and_missing_source(repo):
    _seed(repo, "src-a", 10)
    store = repo._runtime.source_store
    with pytest.raises(ValueError):
        store.source_elements_page("src-a", positions=[0] * (MAX_POSITIONS_PER_READ + 1))
    with pytest.raises(ValueError):
        store.source_elements_page("src-a", anchor_element_id="x", positions=[0])
    with pytest.raises(KeyError):
        store.source_elements_page("src-missing", positions=[0, 1])
    with pytest.raises(KeyError):
        store.source_elements_page("src-missing", positions=[])
    empty = store.source_elements_page("src-a", positions=[])
    assert empty.items == [] and empty.total_count == 10


def test_facade_forwards_positions(repo):
    _seed(repo, "src-a", 30)
    expected = repo._runtime.source_store.source_elements_page("src-a", positions=[29, 0])
    assert repo.source_elements_page("src-a", positions=[29, 0]) == expected


def test_one_positional_read_is_one_statement(repo):
    _seed(repo, "src-a", 150)
    store = repo._runtime.source_store
    statements: list[str] = []
    db = store.database.connect()
    db.set_trace_callback(statements.append)
    try:
        store.source_elements_page("src-a", positions=list(range(0, 150, 2)))
    finally:
        db.set_trace_callback(None)
    assert len(statements) == 1, statements


def _plan(db, sql, params):
    return [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]


def test_every_position_keeps_the_windowed_reads_plan(repo):
    _seed(repo, "src-a", 150)
    store = repo._runtime.source_store
    statements: list[str] = []
    db = store.database.connect()
    windowed = _plan(
        db,
        "SELECT * FROM source_elements WHERE source_id=? "
        f"ORDER BY {READING_ORDER_SQL} LIMIT ? OFFSET ?",
        ("src-a", 1, 70),
    )
    assert any("idx_source_elements_reading_order" in line for line in windowed), windowed
    db.set_trace_callback(statements.append)
    try:
        store.source_elements_page("src-a", positions=[3, 70, 149])
    finally:
        db.set_trace_callback(None)
    (sql,) = statements
    plan = db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    scans = [row[3] for row in plan if "source_elements" in row[3]]
    # one term per position plus the header's COUNT; each term's scan line is
    # the windowed read's scan line
    term_scans = [line for line in scans if "idx_source_elements_reading_order" in line]
    assert len(term_scans) == 3, plan
    assert windowed == [term_scans[0]] and len(set(term_scans)) == 1, (term_scans, windowed)
    assert not any(line.startswith("SCAN source_elements") for line in scans), plan


class _CountingReads:
    def __init__(self, store):
        self.store = store
        self.calls: list[tuple] = []

    def source_elements_page(self, source_id, offset=0, limit=1, *, positions=None):
        if positions is None:
            self.calls.append(("offset", offset))
            return self.store.source_elements_page(source_id, offset=offset, limit=limit)
        self.calls.append(("positions", len(positions)))
        return self.store.source_elements_page(source_id, positions=positions)


def assert_promised_positions(result, ordered, max_elements, coverage, calls):
    """The overview's citations, located in the store's own element order."""
    where = {element.id: index for index, element in enumerate(ordered)}
    cited = [where[citation.element_id] for citation in result.citations]
    sampled = min(len(ordered), max_elements)
    assert cited == sorted(set(cited))
    # A tight budget drops samples whose share cannot hold their breadcrumb, so
    # the citations are a subsequence of the sampled positions; with room for
    # all of them they are exactly those positions.
    if coverage == "opening":
        assert all(index < sampled for index in cited)
        assert len(cited) < sampled or cited == list(range(sampled))
    if len(cited) == sampled and coverage == "spread" and sampled:
        assert cited[-1] == len(ordered) - 1
    if len(cited) == sampled and coverage == "spread" and sampled > 1:
        gaps = [b - a for a, b in zip(cited, cited[1:])]
        assert cited[0] == 0 and max(gaps) - min(gaps) <= 1
    later = max_elements - 1 if max_elements > 1 or coverage == "opening" else 1
    assert calls[0] == ("offset", 0)
    assert len(calls) == 1 + math.ceil(later / MAX_POSITIONS_PER_READ)
    assert sum(n for kind, n in calls if kind == "positions") == later


@pytest.mark.parametrize("max_elements", [1, 2, 5, 22, 64, 150, 230])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
@pytest.mark.parametrize("budget", [120, 10_000_000])
def test_overview_cites_the_promised_positions_in_batched_reads(
    repo, max_elements, coverage, budget,
):
    notebook_id = _seed(repo, "src-a", 260)
    store = repo._runtime.source_store
    item = SourceItem("src-a", "文档", "文档", "", notebook_id, "personal")
    reads = _CountingReads(store)
    result = prepare_source_overview(
        reads, item, budget, max_elements, active_notebook_id=notebook_id,
        generation_reader=lambda _: "v1", coverage=coverage,
    )
    assert len(result.context_block) <= budget
    if budget > 120:
        assert len(result.citations) == min(260, max_elements)
    assert_promised_positions(
        result, store.source_elements("src-a"), max_elements, coverage, reads.calls,
    )


@pytest.mark.parametrize("positions,shape", [
    ([10**5 - 1], "per_offset"),            # the last element only: never the head walk
    ([5, 5, 5], "per_offset"),
    (list(range(1, 63)), "per_offset"),     # opening: offsets sum 1953 < 2048
    (list(range(1, 64)), "head_walk"),      # opening: offsets sum 2079 >= 2048
    ([i * 99_999 // 63 for i in range(1, 64)], "head_walk"),
    ([50_000, 99_999], "per_offset"),       # 1.5 walks: under the 2x margin
    ([33_333, 66_666, 99_999], "head_walk"),
    (list(range(1, 100)), "head_walk"),
    ([], "per_offset"),
])
def test_postgres_shape_choice(positions, shape):
    """The head walk only where the per-offset reads would walk at least twice
    its length and enough rows for its window and probes to pay off."""
    from app.repositories.postgres.source_store import SourceStore as PgStore

    chosen = PgStore._positions_sql(positions)
    assert chosen == {
        "per_offset": PgStore._POSITIONS_PER_OFFSET_SQL,
        "head_walk": PgStore._POSITIONS_HEAD_WALK_SQL,
    }[shape]

