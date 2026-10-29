"""A source's elements in reading order past 9 999 elements, on SQLite (the
PostgreSQL twin is ``tests/postgres/test_source_element_reading_order_pg.py``;
the checks live in ``tests/source_element_reading_order_cases.py``).

Also pins the single definition: the Python twin ``index_digits`` against the
SQL expression, and every frozen copy of that expression (SQLite v105, the
snapshot verifier, PostgreSQL 0086) against the live one the readers use.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories import element_reading_order as order
from app.repositories.sqlite import migrations as sqlite_migrations
from app.services.repository_factory import create_repository
from tests import source_element_reading_order_cases as cases

_ROOT = Path(__file__).resolve().parents[2]


def _executemany(db, sql, rows):
    db.executemany(sql, rows)


BACKEND = cases.Backend(
    name="sqlite", placeholder="?", timestamp=lambda value: value,
    executemany=_executemany,
)


def _repo(directory: Path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{directory / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(directory / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    return create_repository(Settings())


@pytest.fixture(scope="module")
def big_world(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        repo = _repo(tmp_path_factory.mktemp("big"), monkeypatch)
        try:
            yield repo, cases.seed(repo, BACKEND, "big")
        finally:
            repo.close()


@pytest.fixture(scope="module")
def small_world(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        repo = _repo(tmp_path_factory.mktemp("small"), monkeypatch)
        try:
            yield repo, cases.seed(repo, BACKEND, "small")
        finally:
            repo.close()


def test_big_source_reads_in_numeric_order(big_world):
    cases.check_big_source_reads_in_numeric_order(*big_world)


def test_first_excerpt_is_the_first_nonblank_element(big_world):
    cases.check_first_excerpt_is_the_first_nonblank_element(big_world[0])


def test_continuation_crosses_digits_and_instants(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    try:
        cases.check_continuation_crosses_digits_and_instants(repo, BACKEND)
    finally:
        repo.close()


def test_step_ranks_stay_inside_the_first_elements_notebook(tmp_path, monkeypatch):
    """Ranks cover only the asked-for elements of the notebook the first one
    belongs to (or the owner notebook); an element of another notebook gets
    none, so a caller sorts it last -- as before the ranks went relative."""
    repo = _repo(tmp_path, monkeypatch)
    try:
        with repo._runtime.database.write() as db:
            for notebook, source, ids in (
                ("nb-ra", "src-ra", ["el-src-ra-0002", "el-src-ra-0001"]),
                ("nb-rb", "src-rb", ["el-src-rb-0001"]),
            ):
                db.execute(
                    "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
                    "created_at,updated_at) VALUES (?,?,'','S','draft','user-local',?,?)",
                    (notebook, notebook, cases.INSTANTS[0], cases.INSTANTS[0]))
                db.execute(
                    "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                    "file_name,file_path,file_size,created_at,updated_at) VALUES "
                    "(?,?,?,'markdown','extracted','parsed','x.md','',1,?,?)",
                    (source, notebook, source, cases.INSTANTS[0], cases.INSTANTS[0]))
                db.executemany(
                    "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                    "text,metadata,created_at) VALUES (?,?,'paragraph','p','t','{}',?)",
                    [(element_id, source, cases.INSTANTS[0]) for element_id in ids])
        probe = ["el-src-ra-0002", "el-src-rb-0001", "el-src-ra-0001"]
        expected = {"el-src-ra-0001": 0, "el-src-ra-0002": 1}
        assert cases.read_step_ordinals(repo, probe) == expected
        assert cases.read_step_ordinals(repo, probe, "nb-ra") == expected
        assert cases.read_step_ordinals(repo, probe, "nb-rb") == {"el-src-rb-0001": 0}
    finally:
        repo.close()


def test_small_sources_read_exactly_as_before(small_world):
    cases.check_small_sources_read_exactly_as_before(*small_world)


@pytest.mark.parametrize(
    "source_id,element_id,digits",
    [
        ("src-a", "el-src-a-0001", 4),
        ("src-a", "el-src-a-9999", 4),
        ("src-a", "el-src-a-10000", 5),
        ("src-a", "el-src-a-123456", 6),
        ("src-a", "el-src-a-0012-g001", 4),
        ("src-a", "el-src-a-10001-g001", 5),
        ("src-a", "el-src-a-late", 0),
        ("src-a", "el-src-a-", 0),
        ("src-a", "el-src-ab-0001", 0),
        ("src-a", "el-src-b-0001", 0),
        ("src-a", "el-kh-0123456789abcdef", 0),
        ("src-promo-x", "el-promo-0123abcd", 0),
        ("src-a", "el-0123456789abcdef0123456789abcdef", 0),
        ("src_a%", "el-src_a%-0042", 4),
    ],
)
def test_python_twin_matches_the_sql_expression(source_id, element_id, digits):
    assert order.index_digits(source_id, element_id) == digits
    with sqlite3.connect(":memory:") as db:
        (value,) = db.execute(
            f"SELECT {order.INDEX_DIGITS_SQL} FROM (SELECT ? AS id, ? AS source_id)",
            (element_id, source_id),
        ).fetchone()
    assert value == digits


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "repository_snapshot_verifier", _ROOT / "scripts" / "verify_repository_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses resolve its string annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_frozen_copy_is_the_live_expression():
    """The readers order by the live constant; SQLite only uses an index whose
    expression is structurally the readers' term, so the migration's frozen
    text, the verifier's and PostgreSQL 0086's must stay the same string."""
    assert sqlite_migrations._V105_INDEX_DIGITS_SQL == order.INDEX_DIGITS_SQL
    module = _load_verifier()
    assert module._V105_INDEX_DIGITS == order.INDEX_DIGITS_SQL
    pg = (_ROOT / "backend/app/repositories/postgres/migrations/"
          "0086_source_element_reading_order.sql").read_text()
    assert pg.count(order.INDEX_DIGITS_SQL) == 2


def test_v105_replaces_the_two_element_indexes(big_world):
    repo, _rows = big_world
    with repo._runtime.database.connect() as db:
        indexes = dict(db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='source_elements' AND sql IS NOT NULL"
        ).fetchall())
    assert set(indexes) == {
        "idx_source_elements_source",
        "idx_source_elements_reading_order",
        "idx_source_elements_type_reading_order",
    }
    assert order.INDEX_DIGITS_SQL in indexes["idx_source_elements_reading_order"]


def test_id_ordered_readers_keep_their_old_statement_where_nothing_moves(
    big_world, small_world,
):
    """``id_order_for``: a parsed source of at most 9 999 elements (images
    included) keeps the old ``ORDER BY id``; a bigger one reads the index
    order; a source written at several instants sorts on the digits."""
    probe = order.id_order_probe_sql()

    def chosen(repo, source_id):
        with repo._runtime.database.connect() as db:
            return order.id_order_for(db.execute(probe, (source_id, source_id)).fetchall())

    assert chosen(small_world[0], cases.SMALL) == "id"
    assert chosen(big_world[0], cases.BIG) == order.reading_order_sql()
    assert chosen(small_world[0], cases.KNOWHOW) == order.id_order_sql()
    assert chosen(small_world[0], "src-missing") == "id"


@pytest.mark.parametrize(
    "first,last,expected",
    [
        (("t0", 4), ("t0", 4), "id"),
        (("t0", 4), ("t0", 5), order.reading_order_sql()),
        (("t0", 0), ("t1", 0), order.id_order_sql()),
        (("t0", 4), ("t1", 5), order.id_order_sql()),
    ],
)
def test_id_order_for_branches(first, last, expected):
    rows = [{"created_at": first[0], "digits": first[1]},
            {"created_at": last[0], "digits": last[1]}]
    assert order.id_order_for(rows) == expected
    assert order.id_order_for(list(reversed(rows))) == expected
    assert order.id_order_for([]) == "id"


def test_tail_merges_the_two_later_ranges_in_key_order():
    """``page_after``'s tail: rows with more digits at the cursor's instant
    come before rows of a later instant, each range bounded by the page."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE source_elements (id TEXT, source_id TEXT, created_at TEXT)")
    rows = [(f"el-s-{i:04d}", "s", "t0") for i in range(1, 6)]
    rows += [(f"el-s-{i}", "s", "t0") for i in (10000, 10001)]
    rows += [("el-s-0001", "s", "t1"), ("el-s-0002", "s", "t1")]
    db.executemany("INSERT INTO source_elements VALUES (?,?,?)", rows)
    statements = order.after_cursor_sqls(("id", "created_at"), "source_id = ?")

    def run(sql, params):
        return db.execute(sql, params).fetchall()

    def page(after, limit):
        return [(r["created_at"], r["id"]) for r in order.page_after(
            run, statements, ("s",), order.reading_order_key("s", *after), limit)]

    assert page(("t0", "el-s-0003"), 3) == [
        ("t0", "el-s-0004"), ("t0", "el-s-0005"), ("t0", "el-s-10000")]
    assert page(("t0", "el-s-0005"), 4) == [
        ("t0", "el-s-10000"), ("t0", "el-s-10001"), ("t1", "el-s-0001"), ("t1", "el-s-0002")]
    assert page(("t0", "el-s-10001"), 1) == [("t1", "el-s-0001")]
    assert page(("t1", "el-s-0002"), 5) == []


def _plans(repo, run) -> list[str]:
    statements: list[str] = []
    db = repo._runtime.database.connect()
    db.set_trace_callback(statements.append)
    try:
        run()
    finally:
        db.set_trace_callback(None)
    return [
        " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
        for sql in statements if "source_elements" in sql
    ]


def test_readers_ride_the_reading_order_index_without_a_sort(big_world):
    """Every paged reader is an index range on the successor index -- no table
    scan, no temporary sort -- including the keyset continuation, which SQLite
    serves as index range seeks (it does not range over an expression)."""
    repo, _rows = big_world
    store = repo._runtime.source_store
    anchor = f"el-{cases.BIG}-1135"

    def run():
        items, after = store.source_elements_after(cases.BIG, None, 50)
        store.source_elements_after(cases.BIG, (after[0], f"el-{cases.BIG}-9990"), 50)
        store.source_elements_page(cases.BIG, offset=9_990, limit=40)
        store.source_elements_page(cases.BIG, limit=40, anchor_element_id=anchor)
        store.source_elements_page(cases.BIG, positions=[0, 10_000])
        store.source_elements(cases.BIG)

    plans = _plans(repo, run)
    assert plans
    for plan in plans:
        assert "SCAN source_elements" not in plan, plan
        # The only sort allowed is the continuation tail's merge of its two
        # page-bounded ranges (at most two pages of rows, usually none).
        if "CO-ROUTINE tail" not in plan:
            assert "TEMP B-TREE" not in plan, plan
    walked = [plan for plan in plans if "idx_source_elements_reading_order" in plan]
    assert any("created_at=? AND <expr>=? AND id>?" in plan for plan in walked), plans
    # The boundary page needs the second range (more digits) as well.
    assert any("created_at=? AND <expr>>?" in plan for plan in walked), plans
