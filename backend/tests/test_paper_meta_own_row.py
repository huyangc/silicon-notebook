"""SQLite runner for ``tests/paper_meta_own_row_cases.py`` plus the SQLite plan
pins of the narrowed readers (must hold on CI's SQLite 3.45 and on 3.53).

PostgreSQL twin: ``tests/postgres/test_paper_meta_own_row_pg.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite import database as sqlite_database
from app.repositories.sqlite.source_store import (
    PAPER_META_ELIGIBLE_SQL,
    PAPER_META_SOURCE_NB_INDEX,
)
from app.services.sqlite_repository import SQLiteRepository
from tests import paper_meta_own_row_cases as cases
from tests.model_testkit import RecordingModelProvider

PH = "?"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(), model_provider=RecordingModelProvider())


def test_backfill_finds_and_heals_a_legacy_row(repo, monkeypatch):
    cases.case_backfill_finds_and_heals_a_legacy_row(repo, PH, monkeypatch)


def test_display_joins_drop_a_legacy_row(repo):
    cases.case_display_joins_drop_a_legacy_row(repo, PH)


def test_well_formed_rows_read_byte_identically(repo):
    cases.case_well_formed_rows_read_byte_identically(
        repo, PH, eligible_sql=PAPER_META_ELIGIBLE_SQL,
        missing_order=" ORDER BY s.created_at ASC",
    )


def _captured_paper_meta_sql(monkeypatch, action) -> list[tuple[str, tuple]]:
    """Every statement touching source_paper_meta that ``action`` issues,
    captured at the connection class (readers open their own connections)."""
    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def spying_execute(self, sql, parameters=(), /):
        if "source_paper_meta" in str(sql):
            captured.append((str(sql), tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", spying_execute)
    try:
        action()
    finally:
        monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    return captured


def _plan(repo, sql: str, params: tuple) -> str:
    with repo._runtime.source_store.database.connect() as db:
        return "\n".join(
            row["detail"] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        )


def test_paper_meta_own_row_plans_stay_covered(repo, monkeypatch):
    """Plan pins. The missing-metadata consumers and the analytics counts probe
    ``idx_source_paper_meta_source_nb`` as a COVERING index (``INDEXED BY``:
    without it SQLite keeps the unique primary key and reads the table row per
    source for notebook_id); the display joins keep the primary-key probe they
    had (they read paper_title from the row anyway, the notebook check is
    ``+``-spelled so it never selects an index). Statement text comes from the
    real calls, not hand copies."""
    world = cases.build_world(repo)
    runtime = repo._runtime
    store = runtime.source_store
    ids = ["s-legacy", "s-good"]

    def covered_readers():
        store.sources_missing_paper_meta(world.notebook_id)
        repo.maintenance.paper_metadata_source_ids_page(world.notebook_id, limit=500)
        with store.database.connect() as db:
            runtime.queries.notebook_paper_meta_missing(db, world.notebook_id)
        runtime.queries.notebook_analytics(world.notebook_id)

    covered = _captured_paper_meta_sql(monkeypatch, covered_readers)
    assert len(covered) == 4, [sql for sql, _ in covered]
    for sql, params in covered:
        assert f"INDEXED BY {PAPER_META_SOURCE_NB_INDEX}" in sql, sql
        plan = _plan(repo, sql, params)
        assert (
            f"USING COVERING INDEX {PAPER_META_SOURCE_NB_INDEX} "
            "(source_id=? AND notebook_id=?)"
        ) in plan, plan
        assert "sqlite_autoindex_source_paper_meta_1" not in plan, plan

    def display_readers():
        with store.database.connect() as db:
            store.source_display_rows(db, ids)
            store.source_listing_rows(db, ids)
        runtime.queries.list_user_activity(world.owner_id, activity_type="source", limit=50)
        with store.database.write() as db:
            runtime.notebook_store._retain_user_activity_before_delete(db, world.notebook_id)

    display = _captured_paper_meta_sql(monkeypatch, display_readers)
    assert len(display) == 4, [sql for sql, _ in display]
    for sql, params in display:
        assert "+m.notebook_id" in sql or "+pm.notebook_id" in sql, sql
        plan = _plan(repo, sql, params)
        assert "USING INDEX sqlite_autoindex_source_paper_meta_1 (source_id=?)" in plan, plan
        assert "idx_source_paper_meta_nb" not in plan, plan


def test_activity_feed_pages_sources_before_joining_metadata(repo, monkeypatch):
    """Shape pin for the SQLite activity feed: it orders and limits ``sources``
    in a subquery and joins ``source_paper_meta`` to that page only. The flat
    ``sources LEFT JOIN source_paper_meta ... ORDER BY ... LIMIT`` form makes
    SQLite probe the child table (and evaluate the own-notebook check) for
    every visible source of the owned notebooks before it sorts -- PostgreSQL
    already joins after its Limit. Checked on the statement text and on the
    plan: the page is a materialized / co-routine subquery, and the scan of
    ``sources`` sits inside it."""
    world = cases.build_world(repo)
    runtime = repo._runtime
    captured = _captured_paper_meta_sql(
        monkeypatch,
        lambda: runtime.queries.list_user_activity(
            world.owner_id, activity_type="source", limit=50),
    )
    assert len(captured) == 1, [sql for sql, _ in captured]
    sql, params = captured[0]
    flat = " ".join(sql.split())
    page_at = flat.index("FROM (SELECT s.id")
    join_at = flat.index("LEFT JOIN source_paper_meta m")
    limit_at = flat.index("LIMIT ?) s")
    assert page_at < limit_at < join_at, flat
    plan = _plan(repo, sql, params)
    assert "CO-ROUTINE" in plan or "MATERIALIZE" in plan, plan


def test_no_hand_copied_paper_meta_predicate_in_the_repositories():
    """Source guard (opus review P1): the missing-metadata predicate and the
    eligibility predicate are spelled once per backend, in source_store
    (PAPER_META_NO_META_SQL / PAPER_META_ELIGIBLE_SQL). A reader that writes its
    own copy is how the offline backfill pager kept the pre-narrowing
    predicate; every reader reuses the constants instead."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "repositories"
    no_meta = re.compile(r"EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+source_paper_meta\b", re.I)
    eligible = re.compile(r"doc_type\s+IN\s*\(\s*''\s*,\s*'academic_paper'\s*\)", re.I)
    found: dict[str, list[str]] = {"no_meta": [], "eligible": []}
    for path in sorted(root.rglob("*.py")):
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        relative = str(path.relative_to(root))
        found["no_meta"] += [relative] * len(no_meta.findall(code))
        found["eligible"] += [relative] * len(eligible.findall(code))
    expected = ["postgres/source_store.py", "sqlite/source_store.py"]
    assert sorted(found["no_meta"]) == expected, found
    assert sorted(found["eligible"]) == expected, found
