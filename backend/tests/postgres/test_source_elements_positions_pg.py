"""PostgreSQL ``source_elements_page(positions=)`` (debt fix F3).

The twin of ``tests/test_source_elements_positions.py``:

* the adapter answers what ``page_at_positions`` answers over its own
  ``source_elements`` (the contract the in-memory doubles reuse);
* one positional read is ONE statement -- existence, count and elements in a
  single READ COMMITTED snapshot -- and it runs inside a read budget (whose
  wrapper sends a ``set_config`` first, the reason the plan dropped a
  ``SET TRANSACTION`` design);
* ``prepare_source_overview`` is byte-identical to the frozen pre-batching
  function, statically and when the document changes after the first page;
* the plan, custom and generic: the head walk is an index scan of
  ``idx_source_elements_source_created`` (only or not -- the visibility map is
  not this pin's business), hits go back by ``pk_source_elements``, and
  nothing scans or sorts ``source_elements``.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
import time
from pathlib import Path

import psycopg
import pytest

from app.domain.source_positions import MAX_POSITIONS_PER_READ, page_at_positions
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.source_store import SourceStore
from app.repositories.read_budget import read_budget
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview
from tests.document_overview_legacy_oracle import legacy_prepare_source_overview

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_source_elements_positions"),
]

NOW = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-02T00:00:00+00:00"
DOC = 260


@pytest.fixture
def database(postgres_settings):
    settings = postgres_settings.model_copy(update={
        "postgres_pool_min_size": 1,
        "postgres_pool_max_size": 1,
        "postgres_pool_acquire_timeout_seconds": 10,
        "postgres_statement_timeout_seconds": 20,
    })
    database = PostgresDatabase(settings, Path(__file__).resolve().parents[3])
    try:
        PostgresMigrator(database).migrate()
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database) -> None:
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('nb','nb',%s,%s)",
            (NOW, NOW),
        )
        for source_id, count in (("src-a", DOC), ("src-b", 40), ("src-empty", 0)):
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


def test_plan_walks_the_order_index_and_probes_hits_by_primary_key(store, database):
    with _statements(database) as seen:
        store.source_elements_page("src-a", positions=[3, 120, DOC - 1])
    [(sql, params)] = seen
    generic_sql = (sql.replace("%(source_id)s", "$1").replace("%(positions)s", "$2")
                   .replace("%(span)s", "$3"))
    assert "%(" not in generic_sql
    with database.connect() as connection:
        custom = _explain(connection, "COSTS OFF", sql, params)
        generic = _explain(connection, "GENERIC_PLAN, COSTS OFF", generic_sql, ())
    for name, plan in (("custom", custom), ("generic", generic)):
        assert re.search(
            r"Index (Only )?Scan using idx_source_elements_source_created on source_elements",
            plan,
        ), (name, plan)
        assert "Index Scan using pk_source_elements on source_elements" in plan, (name, plan)
        assert "Seq Scan on source_elements" not in plan, (name, plan)
        assert "Bitmap Heap Scan on source_elements" not in plan, (name, plan)
        # the only sort is the final one, by the caller's ordinal
        assert re.findall(r"Sort Key: (.*)", plan) == ["w.ord"], (name, plan)


def _fields(result) -> str:
    return json.dumps([
        result.context_block, json.dumps(result.id_map, ensure_ascii=False),
        [citation.model_dump_json() for citation in result.citations],
        result.coverage_note,
    ], ensure_ascii=False)


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
@pytest.mark.parametrize("budget", [120, 30_000])
def test_overview_is_byte_identical_and_batched(store, max_elements, coverage, budget):
    reads = _Reads(store)
    current = prepare_source_overview(
        reads, ITEM, budget, max_elements, active_notebook_id="nb",
        generation_reader=lambda _: "v1", coverage=coverage,
    )
    legacy = legacy_prepare_source_overview(
        store, ITEM, budget, max_elements, active_notebook_id="nb",
        generation_reader=lambda _: "v1", coverage=coverage,
    )
    assert _fields(current) == _fields(legacy)
    later = max_elements - 1 if max_elements > 1 or coverage == "opening" else 1
    assert len(reads.calls) == 1 + math.ceil(later / MAX_POSITIONS_PER_READ)


def _write(database, sql, params=()):
    with database.write() as db:
        db.execute(sql, params)


CHANGES = {
    "shrink": ("DELETE FROM source_elements WHERE source_id='src-a' AND created_at=%s",
               (LATER,)),
    "grow": ("INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
             "created_at) VALUES ('el-src-a-late','src-a','paragraph','p','late',%s)",
             (LATER,)),
    "delete": ("DELETE FROM sources WHERE id='src-a'", ()),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("max_elements", [5, 230])
def test_a_change_after_the_first_page_is_byte_identical(
    database, store, change, max_elements,
):
    def run(fn):
        def apply():
            _write(database, *CHANGES[change])
        try:
            return _fields(fn(
                _Reads(store, apply), ITEM, 30_000, max_elements,
                active_notebook_id="nb", generation_reader=lambda _: "v1",
            ))
        except KeyError as exc:
            return f"KeyError:{exc}"

    current = run(prepare_source_overview)
    _restore(database)
    legacy = run(legacy_prepare_source_overview)
    assert current == legacy
    expected = "KeyError:'src-a'" if change == "delete" else "读取期间文档内容发生变化"
    assert expected in current


def _restore(database) -> None:
    """Back to the seeded state, so the frozen function reads what the
    current one read before the same change."""
    with database.write() as db:
        db.execute("TRUNCATE source_elements, sources, notebooks CASCADE")
    _seed(database)


def test_a_reparse_between_reads_is_byte_identical(store):
    for fn in (prepare_source_overview, legacy_prepare_source_overview):
        generations = iter(["v1", "v2"])
        result = fn(_Reads(store), ITEM, 30_000, 5, active_notebook_id="nb",
                    generation_reader=lambda _: next(generations))
        assert result.coverage_note == "读取期间文档重新解析，无法确认原文一致性，请重试。"
