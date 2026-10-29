"""A source's elements in reading order past 9 999 elements, on real
PostgreSQL: the twin of ``tests/test_source_element_reading_order.py``,
running the same checks (``tests/source_element_reading_order_cases.py``),
plus the PostgreSQL-only facts -- the Python twin of the digits expression
against the server's evaluation, migration 0086's index shapes and the plans
of the paged readers."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories import element_reading_order as order
from app.repositories.postgres import source_store as pg_source_store
from app.services.repository_factory import create_repository
from tests import source_element_reading_order_cases as cases
from tests.postgres.conftest import _isolated_postgres_scope, vacuum_until_all_visible

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_source_element_reading_order"),
]


def _executemany(db, sql, rows):
    with db.cursor() as cursor:
        cursor.executemany(sql, rows)


BACKEND = cases.Backend(
    name="postgres", placeholder="%s", timestamp=lambda value: value,
    executemany=_executemany,
)


def _world(tmp_path_factory, name: str):
    import os

    base = os.environ.get("TEST_POSTGRES_URL")
    if not base:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with _isolated_postgres_scope(base) as scope, pytest.MonkeyPatch.context() as patch:
        directory = tmp_path_factory.mktemp(name)
        patch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
        patch.setenv("DATABASE_URL", scope.url)
        patch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(directory / "storage"))
        patch.setenv("EVENT_LOG_ENABLED", "false")
        patch.setenv("LLM_LOG_ENABLED", "false")
        patch.setenv("MODEL_SERVICES_CONFIG", "")
        repo = create_repository(Settings())
        try:
            rows = cases.seed(repo, BACKEND, name)
            # A settled table (statistics, a full visibility map), the state
            # the plan pins below speak about; the order checks do not care.
            vacuum_until_all_visible(scope.url, "source_elements")
            with repo._runtime.database.write() as db:
                db.execute("ANALYZE source_elements")
            yield repo, rows
        finally:
            repo.close()


@pytest.fixture(scope="module")
def big_world(tmp_path_factory):
    yield from _world(tmp_path_factory, "big")


@pytest.fixture(scope="module")
def small_world(tmp_path_factory):
    yield from _world(tmp_path_factory, "small")


def test_big_source_reads_in_numeric_order_pg(big_world):
    cases.check_big_source_reads_in_numeric_order(*big_world)


def test_first_excerpt_is_the_first_nonblank_element_pg(big_world):
    cases.check_first_excerpt_is_the_first_nonblank_element(big_world[0])


def test_continuation_crosses_digits_and_instants_pg(small_world):
    cases.check_continuation_crosses_digits_and_instants(small_world[0], BACKEND)


def test_small_sources_read_exactly_as_before_pg(small_world):
    cases.check_small_sources_read_exactly_as_before(*small_world)


def test_python_twin_matches_the_server_expression(big_world):
    repo, _rows = big_world
    probes = [
        ("src-a", "el-src-a-0001"), ("src-a", "el-src-a-10000"),
        ("src-a", "el-src-a-123456"), ("src-a", "el-src-a-0012-g001"),
        ("src-a", "el-src-a-10001-g001"), ("src-a", "el-src-a-late"),
        ("src-a", "el-src-a-"), ("src-a", "el-src-ab-0001"),
        ("src-a", "el-kh-0123456789abcdef"), ("src-promo-x", "el-promo-0123abcd"),
        ("src_a%", "el-src_a%-0042"),
    ]
    with repo._runtime.database.connect() as db:
        for source_id, element_id in probes:
            row = db.execute(
                f"SELECT {order.INDEX_DIGITS_SQL} AS digits "
                "FROM (SELECT %s::text AS id, %s::text AS source_id) AS probe",
                (element_id, source_id),
            ).fetchone()
            assert row["digits"] == order.index_digits(source_id, element_id), element_id


def test_0085_replaces_the_two_element_indexes(big_world):
    repo, _rows = big_world
    with repo._runtime.database.connect() as db:
        names = {
            row["indexname"] for row in db.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='source_elements' "
                "AND schemaname=current_schema()"
            ).fetchall()
        }
    assert "idx_source_elements_reading_order" in names
    assert "idx_source_elements_type_reading_order" in names
    assert "idx_source_elements_source_created" not in names
    assert "idx_source_elements_source_type" not in names


def _explain(db, sql: str, params) -> str:
    return "\n".join(
        str(next(iter(row.values())))
        for row in db.execute("EXPLAIN (COSTS OFF) " + sql, params).fetchall()
    )


def test_paged_readers_walk_the_reading_order_index(big_world):
    """A keyset continuation's head range is an index range on the successor
    index ordered by ``id`` alone (created_at and the digits fixed by
    equality), so it neither sorts nor projects the digits expression; the
    tail's two ranges are index ranges too. The OFFSET window walks the index
    index-only (the digits key read from the index, never computed per skipped
    row) with the window's rows back by primary key."""
    repo, _rows = big_world
    head, tail = pg_source_store._AFTER_PAGE_SQLS
    with repo._runtime.database.connect() as db:
        keyset = _explain(db, head, (cases.BIG, cases.INSTANTS[0], 4,
                                     f"el-{cases.BIG}-9990", 50))
        rest = _explain(db, tail, (cases.BIG, cases.INSTANTS[0], 4, 50,
                                   cases.BIG, cases.INSTANTS[0], 50, 50))
        window = _explain(db, pg_source_store._PAGE_SQL, (cases.BIG, 40, 9_990))
    assert "Index Scan using idx_source_elements_reading_order" in keyset, keyset
    assert "Sort" not in keyset, keyset
    assert rest.count("using idx_source_elements_reading_order") == 2, rest
    assert "Seq Scan" not in rest, rest
    assert "Index Only Scan using idx_source_elements_reading_order" in window, window
    assert "Index Scan using pk_source_elements" in window, window
    assert "Seq Scan" not in window, window
