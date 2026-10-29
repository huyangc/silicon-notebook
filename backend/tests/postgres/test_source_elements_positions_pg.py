"""PostgreSQL ``source_elements_page(positions=)`` (debt fix F3).

The twin of ``tests/test_source_elements_positions.py``:

* the adapter answers what ``page_at_positions`` answers over its own
  ``source_elements`` (the contract the in-memory doubles reuse);
* one positional read is ONE statement -- existence, count and elements in a
  single READ COMMITTED snapshot -- and it runs inside a read budget (whose
  wrapper sends a ``set_config`` first, the reason the plan dropped a
  ``SET TRANSACTION`` design);
* ``prepare_source_overview`` cites the promised positions of the stored
  order in batched reads, and shows the visible branch when the document
  shrinks, grows, is deleted or re-parsed after the first page;
* both statement shapes, custom and generic plan, in both visibility-map
  states (``bystander_snapshot``): the per-offset shape is the windowed read's
  ``Limit`` over ``idx_source_elements_reading_order`` for every position
  whatever the visibility map says; with a verified-full map the head walk is
  one index scan of that index with hits back by ``pk_source_elements`` and
  nothing scans or sorts ``source_elements``.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path

import psycopg
import pytest

from app.domain.source_positions import MAX_POSITIONS_PER_READ, page_at_positions
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.source_store import SourceStore
from app.core.config import Settings
from app.repositories.read_budget import read_budget
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview
from tests.postgres.conftest import (
    scans_with_own_conditions,
    vacuum_until_all_visible,
)
from tests.test_source_elements_positions import assert_promised_positions

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_source_elements_positions"),
]

NOW = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-02T00:00:00+00:00"
DOC = 260


CHANGES = {
    "shrink": lambda sid: ("DELETE FROM source_elements WHERE source_id=%s AND created_at=%s",
                           (sid, LATER)),
    "grow": lambda sid: ("INSERT INTO source_elements(id,source_id,element_type,location_label,"
                         "text,created_at) VALUES (%s,%s,'paragraph','p','late',%s)",
                         (f"el-{sid}-late", sid, LATER)),
    "delete": lambda sid: ("DELETE FROM sources WHERE id=%s", (sid,)),
}
CHANGE_CASES = [(change, max_elements) for change in sorted(CHANGES) for max_elements in (5, 230)]


def _change_source(change: str, max_elements: int) -> str:
    return f"src-chg-{change}-{max_elements}"


@pytest.fixture(scope="module")
def database():
    """One seeded world for the whole module (development.md: an immutable
    matrix is not rebuilt per row). Read-only tests use ``src-a``/``src-b``/
    ``src-empty``; each change test writes only to its own ``src-chg-*``."""
    import os

    from tests.postgres.conftest import _isolated_postgres_scope

    base = os.environ.get("TEST_POSTGRES_URL")
    if not base:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with _isolated_postgres_scope(base) as scope:
        database = PostgresDatabase(Settings(
            database_url=scope.url,
            postgres_pool_min_size=1,
            postgres_pool_max_size=1,
            postgres_pool_acquire_timeout_seconds=10,
            postgres_statement_timeout_seconds=20,
            postgres_chunk_fts_timeout_seconds=1.0,
            postgres_lock_timeout_seconds=1,
        ), Path(__file__).resolve().parents[3])
        try:
            PostgresMigrator(database).migrate()
            _seed(database)
            yield database
        finally:
            database.close()


def _seed(database, *, held: bool = False) -> None:
    with database.write() as db:
        # The held-bystander case seeds through the function-scoped
        # ``postgres_database`` (2 s statement timeout, not the module
        # world's 20 s): the 20 000-row bulk insert below fires a per-row sync
        # capture trigger and takes ~0.3 s locally, past 2 s on a busy 4-vCPU
        # CI runner. Every other plan pin's seed lifts the limit the same way.
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('nb','nb',%s,%s)",
            (NOW, NOW),
        )
        documents = [("src-a", DOC), ("src-b", 40), ("src-empty", 0)] + [
            (_change_source(*case), DOC) for case in CHANGE_CASES
        ]
        for source_id, count in documents:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
                "created_at,updated_at) VALUES (%s,'nb',%s,'file','ready','ready',%s,%s)",
                (source_id, source_id, NOW, NOW),
            )
            # Two created_at values and ids whose text order is not insertion
            # order, so "position" really is (created_at, id COLLATE "C").
            for index in range(count):
                db.execute(
                    "INSERT INTO source_elements(id,source_id,element_type,location_label,"
                    "text,metadata,created_at) VALUES (%s,%s,'paragraph',%s,%s,%s::jsonb,%s)",
                    (f"el-{source_id}-{(index * 37) % count:05d}-{index}", source_id,
                     f"第{index + 1}页", f"原文 {index} " * (1 + index % 9),
                     json.dumps({"section_path": f"章{index // 10}"}) if index % 2 else "{}",
                     NOW if index < count // 3 else LATER),
                )
        # Bulk neighbours so the planner costs a real table.
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) SELECT 'src-bulk-'||g,'nb','b','file','ready','ready',"
            "%s,%s FROM generate_series(1,200) g", (NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "created_at) SELECT 'elb-'||s||'-'||g,'src-bulk-'||s,'paragraph','p',"
            "repeat('x',200),%s FROM generate_series(1,200) s, generate_series(1,100) g",
            (NOW,),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) sources, source_elements")
    if not held:
        _settle(database)


def _settle(database) -> None:
    """VACUUM ``sources`` and ``source_elements`` until their visibility maps
    are full -- verified, not assumed (see the EXPLAIN pin)."""
    for table in ("sources", "source_elements"):
        vacuum_until_all_visible(database.settings.database_url, table)


@pytest.fixture
def store(database):
    return SourceStore(database, now=lambda: NOW)


@contextlib.contextmanager
def _statements(database):
    """Record every statement sent through ``database.connect()``."""
    seen: list[tuple[str, object]] = []
    original = database.connect

    @contextlib.contextmanager
    def connect():
        with original() as connection:
            yield _Recording(connection, seen)

    database.connect = connect
    try:
        yield seen
    finally:
        database.connect = original


class _Recording:
    def __init__(self, connection, seen):
        self._connection, self._seen = connection, seen

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, query, params=None, **kwargs):
        self._seen.append((query, params))
        return self._connection.execute(query, params, **kwargs)


POSITION_CASES = [
    [], [0], [DOC - 1], [DOC], [DOC + 1, 0], [5, 5, 5], [DOC - 1, 0, 75, 3],
    list(range(0, DOC, 3)) + [DOC - 1], list(range(100)), [10**9], [-4, 2],
]


@pytest.mark.parametrize("positions", POSITION_CASES)
def test_positions_answer_exactly_what_the_reference_answers(store, positions):
    expected = page_at_positions(store.source_elements("src-a"), positions)
    assert store.source_elements_page("src-a", positions=positions) == expected


def test_refusals_missing_and_empty_sources(store):
    with pytest.raises(ValueError):
        store.source_elements_page("src-a", positions=[0] * (MAX_POSITIONS_PER_READ + 1))
    with pytest.raises(ValueError):
        store.source_elements_page("src-a", anchor_element_id="x", positions=[0])
    with pytest.raises(KeyError):
        store.source_elements_page("src-missing", positions=[0, 1])
    with pytest.raises(KeyError):
        store.source_elements_page("src-missing", positions=[])
    empty = store.source_elements_page("src-empty", positions=[0, 3])
    assert empty.items == [] and empty.total_count == 0
    none = store.source_elements_page("src-a", positions=[])
    assert none.items == [] and none.total_count == DOC


def test_one_positional_read_is_one_statement_and_runs_under_a_read_budget(store, database):
    with _statements(database) as seen:
        page = store.source_elements_page("src-a", positions=[0, 77, DOC - 1])
    assert len(seen) == 1, seen
    with read_budget(time.monotonic() + 30):
        budgeted = store.source_elements_page("src-a", positions=[0, 77, DOC - 1])
    assert budgeted == page and len(page.items) == 3


def _explain(connection, options: str, sql: str, params) -> str:
    rows = connection.execute(f"EXPLAIN ({options}) {sql}", params).fetchall()
    return "\n".join(next(iter(row.values())) for row in rows)


def _index_keyed(plan: str) -> bool:
    """Every scan node of ``source_elements`` carries an Index/Recheck Cond on
    the source or on the element id: no scan reads the table unkeyed."""
    scans = scans_with_own_conditions(plan, "source_elements")
    return bool(scans) and all(
        any(line.split(":", 1)[0] in ("Index Cond", "Recheck Cond")
            and ("source_id = " in line or "(id = " in line)
            for line in own)
        for _node, own in scans
    )


@pytest.mark.parametrize("shape,positions", [
    ("per_offset", [3, 120, DOC - 1]),
    ("per_offset", [DOC - 1]),
    ("head_walk", list(range(0, DOC, 3)) + [DOC - 1]),
    ("head_walk", list(range(1, 100))),
])
def test_both_shapes_walk_the_order_index_and_never_scan_or_sort_elements(
    bystander_snapshot, request, shape, positions,
):
    """Two states (``bystander_snapshot``), because the plan around the walk
    moves with the visibility map. With no page all-visible (a transaction on
    the same database older than the seed -- a parallel lane's session, an
    autoanalyze snapshot) the ``counted`` leg's Index Only Scan is costed as
    heap visits and becomes a Bitmap Heap Scan, and a head walk over the whole
    document becomes a Bitmap Heap Scan + Sort: this pin used to go red on
    those (per_offset included, by the count leg) whenever the module's one
    VACUUM ran under such a snapshot. Now:

    * both states: the per-offset walk is the ``Limit`` over the order index
      for every position, the head walk numbers rows with a ``WindowAgg`` and
      fetches hits by ``pk_source_elements``, every scan of ``source_elements``
      is keyed (no Seq Scan), and per-offset sorts nothing but ``w.ord``;
    * no bystander, the module's world VACUUMed until its visibility map is
      full (checked again right here: a change test may have run first): the
      exact plan -- no Bitmap Heap Scan, the only sort the final ``w.ord``.
    """
    if bystander_snapshot.held:
        database = request.getfixturevalue("postgres_database")
        PostgresMigrator(database).migrate()
        _seed(database, held=True)
    else:
        database = request.getfixturevalue("database")
        _settle(database)
    store = SourceStore(database, now=lambda: NOW)
    expected_sql = {
        "per_offset": SourceStore._POSITIONS_PER_OFFSET_SQL,
        "head_walk": SourceStore._POSITIONS_HEAD_WALK_SQL,
    }[shape]
    with _statements(database) as seen:
        page = store.source_elements_page("src-a", positions=positions)
    [(sql, params)] = seen
    assert sql == expected_sql
    assert page == page_at_positions(store.source_elements("src-a"), positions)
    generic_sql = (sql.replace("%(source_id)s", "$1").replace("%(positions)s", "$2")
                   .replace("%(span)s", "$3"))
    assert "%(" not in generic_sql
    with database.connect() as connection:
        bystander_snapshot.assert_visibility_map_blocked(
            connection, "sources", "source_elements")
        custom = _explain(connection, "COSTS OFF", sql, params)
        generic = _explain(connection, "GENERIC_PLAN, COSTS OFF", generic_sql, ())
    for name, plan in (("custom", custom), ("generic", generic)):
        assert "Seq Scan on source_elements" not in plan, (name, plan)
        assert _index_keyed(plan), (name, plan)
        if shape == "per_offset":
            # each position: a Limit over the order index that picks the id
            # (index-only where the visibility map allows), the element back
            # by primary key
            assert re.search(
                r"Limit\n\s+->  Index (Only )?Scan using "
                r"idx_source_elements_reading_order on source_elements", plan,
            ), (name, plan)
            assert "Index Scan using pk_source_elements on source_elements" in plan, (
                name, plan)
            assert "WindowAgg" not in plan, (name, plan)
            assert re.findall(r"Sort Key: (.*)", plan) == ["w.ord"], (name, plan)
        else:
            assert "WindowAgg" in plan, (name, plan)
            assert "Index Scan using pk_source_elements on source_elements" in plan, (
                name, plan)
        if bystander_snapshot.held:
            continue
        assert "Bitmap Heap Scan on source_elements" not in plan, (name, plan)
        # the only sort is the final one, by the caller's ordinal
        assert re.findall(r"Sort Key: (.*)", plan) == ["w.ord"], (name, plan)
        if shape == "head_walk":
            assert re.search(
                r"Index (Only )?Scan using idx_source_elements_reading_order "
                r"on source_elements", plan,
            ), (name, plan)


class _Reads:
    """Count the adapter calls; ``after_first`` runs once the first page is read."""

    def __init__(self, store, after_first=None):
        self.store, self.after_first, self.calls = store, after_first, []

    def source_elements_page(self, source_id, offset=0, limit=1, *, positions=None):
        if positions is None:
            page = self.store.source_elements_page(source_id, offset=offset, limit=limit)
        else:
            page = self.store.source_elements_page(source_id, positions=positions)
        self.calls.append(("offset" if positions is None else "positions",
                           offset if positions is None else len(positions)))
        if len(self.calls) == 1 and self.after_first is not None:
            self.after_first()
        return page


ITEM = SourceItem("src-a", "文档", "文档", "", "nb", "personal")


@pytest.mark.parametrize("max_elements", [1, 2, 5, 64, 230])
@pytest.mark.parametrize("coverage", ["spread", "opening"])
@pytest.mark.parametrize("budget", [120, 10_000_000])
def test_overview_cites_the_promised_positions_in_batched_reads(
    store, max_elements, coverage, budget,
):
    reads = _Reads(store)
    result = prepare_source_overview(
        reads, ITEM, budget, max_elements, active_notebook_id="nb",
        generation_reader=lambda _: "v1", coverage=coverage,
    )
    assert len(result.context_block) <= budget
    if budget > 120:
        assert len(result.citations) == min(DOC, max_elements)
    assert_promised_positions(
        result, store.source_elements("src-a"), max_elements, coverage, reads.calls,
    )


def _write(database, sql, params=()):
    with database.write() as db:
        db.execute(sql, params)


@pytest.mark.parametrize("change,max_elements", CHANGE_CASES)
def test_a_change_after_the_first_page_shows_its_branch(
    database, store, change, max_elements,
):
    source_id = _change_source(change, max_elements)

    def apply():
        _write(database, *CHANGES[change](source_id))

    item = SourceItem(source_id, "文档", "文档", "", "nb", "personal")
    reads = _Reads(store, apply)
    if change == "delete":
        with pytest.raises(KeyError):
            prepare_source_overview(reads, item, 30_000, max_elements,
                                    active_notebook_id="nb", generation_reader=lambda _: "v1")
        return
    result = prepare_source_overview(reads, item, 30_000, max_elements,
                                     active_notebook_id="nb", generation_reader=lambda _: "v1")
    assert result.coverage_note == "读取期间文档内容发生变化，请重试。"
    assert not result.context_block and not result.id_map and not result.citations


def test_a_reparse_between_reads_discards_the_evidence(store):
    generations = iter(["v1", "v2"])
    result = prepare_source_overview(
        _Reads(store), ITEM, 30_000, 5, active_notebook_id="nb",
        generation_reader=lambda _: next(generations),
    )
    assert result.coverage_note == "读取期间文档重新解析，无法确认原文一致性，请重试。"
    assert not result.context_block and not result.citations
